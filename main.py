from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import logging
import re
import sys
import traceback
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, Iterable, List, Optional, Set, Tuple

from core.constants import (
    DEFAULT_CAPTURE_BACKEND,
    DEFAULT_CAPTURE_MODE,
    DEFAULT_CAPTURE_SCOPE,
    DEFAULT_CAPTURE_STATE_DIR,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_PATH,
    DEFAULT_DIFY_APPLY_CAPTURE_SCOPE,
    DEFAULT_DIFY_USE_DAILY_SETTLEMENT,
    DEFAULT_DIFY_USE_HASH_TIME_WINDOW,
    DEFAULT_DOTENV_PATH,
    DEFAULT_JSON_OUTPUT_PATH,
    DEFAULT_KNOWN_STORE_LOOKUP_ENABLED,
    DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES,
    DEFAULT_KNOWN_STORE_LOOKUP_PATH,
    DEFAULT_LOG_DIR,
    DEFAULT_LOG_FILE_PREFIX,
    DEFAULT_LOGGER_NAME,
    DEFAULT_MANUAL_END_TOKEN,
    DEFAULT_MANUAL_ORDER_ID_SAMPLE,
    DEFAULT_MAX_MESSAGES,
    DEFAULT_MESSAGE_HASH_STATE_PATH,
    DEFAULT_ORDER_ID_DIGITS,
    DEFAULT_ORDER_ID_REQUIRE_HASH,
    DEFAULT_PREFIX,
    DEFAULT_START_MARKER,
    resolve_end_marker,
    resolve_start_marker,
)
from core.monitor import MonitorError, fetch_recent_messages
from core.monitor_win32 import Win32MonitorError, fetch_recent_messages_win32_clipboard
from core.monitor_win32_memory import (
    Win32MemoryMonitorError,
    fetch_recent_messages_win32_memory,
)
from core.parser import parse_messages
from core.store_lookup import annotate_records_with_known_stores, build_known_store_lookup
from setup import check_environment

CSV_HEADERS = [
    "timestamp",
    "order_id",
    "item",
    "amount",
    "raw_message",
    "message_hash",
    "message_captured_at",
    "recorded_at",
]
TraceFn = Optional[Callable[[str], None]]
CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
DATETIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
)
INT_TEXT_RE = re.compile(r"^[+-]?\d+$")
FLOAT_TEXT_RE = re.compile(r"^[+-]?\d+\.\d+$")


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """读取 YAML 配置。"""
    import yaml

    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在：{path}")

    with path.open("r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    return config


def load_dotenv(env_path: str = DEFAULT_DOTENV_PATH) -> Dict[str, str]:
    """读取 .env 文件（轻量解析，不依赖 python-dotenv）。"""
    path = Path(env_path)
    if not path.exists():
        return {}

    data: Dict[str, str] = {}
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue

            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue

            if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
                value = value[1:-1]
            else:
                # 允许非引号值后写注释：FOO=bar # comment
                if " #" in value:
                    value = value.split(" #", 1)[0].rstrip()

            data[key] = value

    return data


def _coerce_env_value(raw_value: str) -> Any:
    text = str(raw_value).strip()
    lowered = text.lower()
    if lowered in {"true", "yes", "on", "1", "是"}:
        return True
    if lowered in {"false", "no", "off", "0", "否"}:
        return False
    if INT_TEXT_RE.fullmatch(text):
        try:
            return int(text)
        except Exception:
            pass
    if FLOAT_TEXT_RE.fullmatch(text):
        try:
            return float(text)
        except Exception:
            pass
    return text


def apply_dotenv_overrides(config: Dict[str, Any], env_data: Dict[str, str]) -> Tuple[Dict[str, Any], int]:
    """把 .env 的键值覆盖到配置中。规则：ENV_KEY -> env_key.lower()。"""
    if not env_data:
        return dict(config), 0

    result = dict(config)
    skip_keys = {"APP_ENV", "DO_NOT_UPLOAD"}
    applied = 0
    for env_key, raw_value in env_data.items():
        if env_key in skip_keys:
            continue
        cfg_key = env_key.strip().lower()
        if not cfg_key:
            continue
        result[cfg_key] = _coerce_env_value(raw_value)
        applied += 1

    return result, applied


def load_runtime_config(
    config_path: str = DEFAULT_CONFIG_PATH,
    env_path: str = DEFAULT_DOTENV_PATH,
) -> Tuple[Dict[str, Any], int]:
    config = load_config(config_path)
    dotenv_data = load_dotenv(env_path)
    return apply_dotenv_overrides(config, dotenv_data)


def ensure_csv_file(csv_path: Path) -> None:
    """确保数据目录与 CSV 文件存在，且含表头。"""
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    if not csv_path.exists() or csv_path.stat().st_size <= 0:
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
            writer.writeheader()
        return

    # 兼容旧版表头：若缺少新字段，则自动迁移到最新表头。
    with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        reader = csv.reader((line.replace("\x00", "") for line in f))
        header = next(reader, [])
    header_set = {str(x).strip() for x in header}
    if all(col in header_set for col in CSV_HEADERS):
        return

    migrated_rows: List[Dict[str, Any]] = []
    with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        dict_reader = csv.DictReader((line.replace("\x00", "") for line in f))
        for row in dict_reader:
            if not isinstance(row, dict):
                continue
            raw_message = _sanitize_csv_text(row.get("raw_message", ""))
            migrated_rows.append(
                {
                    "timestamp": _sanitize_csv_text(row.get("timestamp", "")),
                    "order_id": _sanitize_csv_text(row.get("order_id", ""))
                    or _extract_order_id_from_message(
                        raw_message,
                        DEFAULT_START_MARKER,
                        DEFAULT_ORDER_ID_DIGITS,
                        DEFAULT_ORDER_ID_REQUIRE_HASH,
                    ),
                    "item": _sanitize_csv_text(row.get("item", "")),
                    "amount": row.get("amount", 0),
                    "raw_message": raw_message,
                    "message_hash": _sanitize_csv_text(row.get("message_hash", "")) or _fingerprint_text(raw_message),
                    "message_captured_at": _sanitize_csv_text(row.get("message_captured_at", "")),
                    "recorded_at": _sanitize_csv_text(row.get("recorded_at", "")),
                }
            )

    temp_path = csv_path.with_suffix(".tmp")
    with temp_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(migrated_rows)
    temp_path.replace(csv_path)


def init_run_logger(config: Dict[str, Any]) -> Tuple[logging.Logger, Path]:
    """初始化本次运行日志文件。"""
    log_dir = Path(str(config.get("log_dir", DEFAULT_LOG_DIR)))
    log_path = log_dir / f"{DEFAULT_LOG_FILE_PREFIX}{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.log"
    logger = logging.getLogger(f"{DEFAULT_LOGGER_NAME}.{log_path.stem}")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    logger.handlers.clear()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_path, encoding="utf-8-sig")
        file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(file_handler)
        return logger, log_path
    except Exception:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        logger.addHandler(stream_handler)
        fallback = Path("[stdout]")
        logger.warning(f"[MAIN] 无法写入日志文件，已降级输出到 stdout。target={log_path}")
        return logger, fallback


def _is_admin() -> bool:
    """判断当前进程是否管理员权限。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _make_trace(logger: logging.Logger) -> Callable[[str], None]:
    """构造抓取过程 trace 回调。"""
    return lambda message: logger.info(message)


def _sanitize_csv_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_TEXT_RE.sub("", text)
    return text.strip()


def _fingerprint_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _to_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _today_text() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _build_order_header_pattern(start_marker: str, digits: int, require_hash: bool) -> re.Pattern[str]:
    marker = re.escape((start_marker or DEFAULT_START_MARKER).strip() or DEFAULT_START_MARKER)
    d = max(1, int(digits))
    if require_hash:
        return re.compile(rf"^[#＃]{marker}(\d{{{d}}})$")
    return re.compile(rf"^[#＃]?{marker}(\d{{{d}}})$")


def _is_valid_order_id_ymd_seq(order_id: str) -> bool:
    value = _sanitize_csv_text(order_id)
    if not re.fullmatch(r"\d{8}", value):
        return False
    # 规则: yymmdd + 两位序号
    try:
        datetime.strptime(value[:6], "%y%m%d")
    except ValueError:
        return False
    return True


def _extract_order_id_from_message(
    message_text: str,
    start_marker: str,
    order_id_digits: int = DEFAULT_ORDER_ID_DIGITS,
    require_hash: bool = DEFAULT_ORDER_ID_REQUIRE_HASH,
) -> str:
    text = _sanitize_csv_text(message_text)
    if not text:
        return ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    pattern = _build_order_header_pattern(start_marker, order_id_digits, require_hash)
    matched = pattern.fullmatch(lines[0])
    if not matched:
        return ""
    order_id = matched.group(1)
    if not _is_valid_order_id_ymd_seq(order_id):
        return ""
    return order_id


def _mark_messages(
    messages: List[Dict[str, str]],
    start_marker: str,
    order_id_digits: int = DEFAULT_ORDER_ID_DIGITS,
    require_hash: bool = DEFAULT_ORDER_ID_REQUIRE_HASH,
) -> List[Dict[str, str]]:
    """为抓取消息补充 message_hash 与 message_captured_at 标记。"""
    marked: List[Dict[str, str]] = []
    captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for message in messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        if not raw_message:
            continue
        order_id = _extract_order_id_from_message(
            raw_message,
            start_marker=start_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
        )
        if not order_id:
            continue
        copied = dict(message)
        copied["message"] = raw_message
        copied["order_id"] = order_id
        copied["message_hash"] = _fingerprint_text(raw_message)
        copied["message_captured_at"] = captured_at
        marked.append(copied)
    return marked


def deduplicate_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """单次运行内按 (时间戳, 来源ID, 原消息, 项目, 数量) 去重。"""
    seen: Set[Tuple[str, str, str, str, str, str]] = set()
    unique_records: List[Dict[str, Any]] = []

    for record in records:
        key = (
            _sanitize_csv_text(record.get("timestamp", "")),
            _sanitize_csv_text(record.get("order_id", "")),
            _sanitize_csv_text(record.get("source_id", "")),
            _sanitize_csv_text(record.get("raw_message", "")),
            _sanitize_csv_text(record.get("message_hash", "")),
            _sanitize_csv_text(record.get("item", "")),
            _sanitize_csv_text(record.get("amount", "")),
        )
        if key in seen:
            continue
        seen.add(key)
        unique_records.append(record)

    return unique_records


def _to_int(value: Any, default: int, minimum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _parse_datetime(text: str) -> Optional[datetime]:
    raw = _sanitize_csv_text(text)
    if not raw:
        return None

    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    normalized = (
        raw.replace("年", "-")
        .replace("月", "-")
        .replace("日", " ")
        .replace("时", ":")
        .replace("分", ":")
        .replace("秒", "")
    )
    normalized = re.sub(r"\s+", " ", normalized).strip(" :-")
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(normalized, fmt)
        except ValueError:
            continue

    return None


def _iter_csv_rows_safely(csv_path: Path) -> Iterable[Dict[str, Any]]:
    if not csv_path.exists():
        return

    with csv_path.open("r", encoding="utf-8-sig", errors="ignore", newline="") as f:
        sanitized_lines = (line.replace("\x00", "") for line in f)
        reader = csv.DictReader(sanitized_lines)
        for row in reader:
            if not isinstance(row, dict):
                continue
            yield row


def _resolve_row_event_time(
    timestamp_text: str,
    recorded_at_text: str,
    fallback: datetime,
) -> datetime:
    ts = _parse_datetime(timestamp_text)
    if ts is not None:
        return ts
    recorded = _parse_datetime(recorded_at_text)
    if recorded is not None:
        return recorded
    return fallback


def _to_minute_bucket(dt: datetime) -> int:
    return int(dt.timestamp() // 60)


def _build_existing_message_minute_index(
    csv_path: Path,
    trace: TraceFn = None,
) -> Dict[str, Set[int]]:
    index: DefaultDict[str, Set[int]] = defaultdict(set)
    if not csv_path.exists():
        return {}

    scanned_rows = 0
    indexed_rows = 0
    fallback_now = datetime.now()
    for row in _iter_csv_rows_safely(csv_path):
        scanned_rows += 1
        raw_message = _sanitize_csv_text(row.get("raw_message", ""))
        if not raw_message:
            continue

        event_time = _resolve_row_event_time(
            _sanitize_csv_text(row.get("timestamp", "")),
            _sanitize_csv_text(row.get("recorded_at", "")),
            fallback_now,
        )
        minute_bucket = _to_minute_bucket(event_time)
        digest = _fingerprint_text(raw_message)
        index[digest].add(minute_bucket)
        indexed_rows += 1

    if trace:
        trace(
            f"[MAIN] CSV历史索引: scanned_rows={scanned_rows}, indexed_rows={indexed_rows}, unique_messages={len(index)}"
        )

    return dict(index)


def _has_minute_in_window(existing_minutes: Set[int], target_minute: int, window_minutes: int) -> bool:
    if not existing_minutes:
        return False
    if window_minutes <= 0:
        return target_minute in existing_minutes
    for minute in existing_minutes:
        if abs(minute - target_minute) <= window_minutes:
            return True
    return False


def filter_records_by_existing_csv(
    records: List[Dict[str, Any]],
    csv_path: Path,
    config: Dict[str, Any],
    trace: TraceFn = None,
) -> List[Dict[str, Any]]:
    """按“同消息 + 时间窗口”过滤，避免已写入内容重复入账。"""
    if not records:
        return records

    enabled = _to_bool(config.get("deduplicate_against_csv", True), True)
    if not enabled:
        if trace:
            trace("[MAIN] 已关闭CSV历史去重。")
        return records

    window_minutes = _to_int(config.get("dedupe_time_window_minutes", 1), 1, minimum=0)
    run_fallback_time = datetime.now()
    existing_index = _build_existing_message_minute_index(csv_path, trace=trace)

    grouped: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
    for record in records:
        raw_message = _sanitize_csv_text(record.get("raw_message", ""))
        if not raw_message:
            continue
        grouped[_fingerprint_text(raw_message)].append(record)

    kept: List[Dict[str, Any]] = []
    skipped_groups = 0
    for digest, group_records in grouped.items():
        sample = group_records[0]
        event_time = _resolve_row_event_time(
            _sanitize_csv_text(sample.get("timestamp", "")),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            run_fallback_time,
        )
        target_minute = _to_minute_bucket(event_time)

        existing_minutes = existing_index.get(digest, set())
        if _has_minute_in_window(existing_minutes, target_minute, window_minutes):
            skipped_groups += 1
            continue

        existing_minutes.add(target_minute)
        existing_index[digest] = existing_minutes
        kept.extend(group_records)

    if trace:
        trace(
            f"[MAIN] CSV历史去重(window={window_minutes}m): input={len(records)}, output={len(kept)}, skipped_groups={skipped_groups}"
        )

    return kept


def summarize_item_totals(csv_path: Path, trace: TraceFn = None) -> Dict[str, float]:
    totals: DefaultDict[str, float] = defaultdict(float)
    if not csv_path.exists():
        return {}

    scanned = 0
    for row in _iter_csv_rows_safely(csv_path):
        scanned += 1
        item = _sanitize_csv_text(row.get("item", ""))
        if not item:
            continue
        amount_text = _sanitize_csv_text(row.get("amount", "0"))
        try:
            amount = float(amount_text)
        except ValueError:
            continue
        totals[item] += amount

    if trace:
        trace(f"[MAIN] 汇总统计: scanned_rows={scanned}, unique_items={len(totals)}")

    return dict(totals)


def print_item_totals(totals: Dict[str, float], top_n: int = 20) -> None:
    if not totals:
        print("[累计] 暂无可统计数据。")
        return

    sorted_items = sorted(totals.items(), key=lambda x: (-x[1], x[0]))
    if top_n > 0:
        sorted_items = sorted_items[:top_n]

    print("[累计] 按项目总数量：")
    for item, amount in sorted_items:
        print(f"  {item}: {amount:.2f}")

def _load_today_seen_messages(state_path: Path, today_key: str) -> Set[str]:
    if not state_path.exists():
        return set()

    try:
        with state_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return set()

    if str(payload.get("date", "")) != today_key:
        return set()

    raw_seen = payload.get("seen", [])
    if not isinstance(raw_seen, list):
        return set()
    return {str(x).strip() for x in raw_seen if str(x).strip()}


def _save_today_seen_messages(state_path: Path, today_key: str, seen: Set[str]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"date": today_key, "seen": sorted(seen)}
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def filter_today_new_messages(
    messages: List[Dict[str, str]], config: Dict[str, Any], trace: TraceFn = None
) -> List[Dict[str, str]]:
    """当天范围过滤：同一天内，同一条原始消息只抓取一次。"""
    scope = str(config.get("capture_scope", DEFAULT_CAPTURE_SCOPE)).strip().lower()
    if scope in {"all", "none", "off"}:
        return messages

    today_key = datetime.now().strftime("%Y-%m-%d")
    state_dir = Path(str(config.get("capture_state_dir", DEFAULT_CAPTURE_STATE_DIR)))
    state_path = state_dir / "seen_messages_today.json"
    seen = _load_today_seen_messages(state_path, today_key)

    filtered: List[Dict[str, str]] = []
    added = 0
    for message in messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        if not raw_message:
            continue

        digest = _fingerprint_text(raw_message)
        if digest in seen:
            continue

        seen.add(digest)
        added += 1
        copied = dict(message)
        copied["message"] = raw_message
        filtered.append(copied)

    _save_today_seen_messages(state_path, today_key, seen)

    if trace:
        trace(
            f"[MAIN] 当天范围过滤(scope={scope}): input={len(messages)}, output={len(filtered)}, added={added}, seen_today={len(seen)}"
        )

    return filtered


def _load_hash_state(state_path: Path) -> Dict[str, int]:
    if not state_path.exists():
        return {}

    try:
        with state_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}

    if not isinstance(payload, dict):
        return {}

    raw_map: Any
    if isinstance(payload.get("hash_last_seen_epoch"), dict):
        raw_map = payload.get("hash_last_seen_epoch")
    else:
        raw_map = payload

    state: Dict[str, int] = {}
    if not isinstance(raw_map, dict):
        return state

    for key, value in raw_map.items():
        digest = _sanitize_csv_text(key)
        if not digest:
            continue
        try:
            seen_epoch = int(value)
        except Exception:
            continue
        if seen_epoch <= 0:
            continue
        state[digest] = seen_epoch

    return state


def _save_hash_state(state_path: Path, state: Dict[str, int]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "hash_last_seen_epoch": state,
    }
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _load_daily_settlement_state(state_path: Path) -> Dict[str, Any]:
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _save_daily_settlement_state(state_path: Path, state: Dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def _build_daily_message_id(message: Dict[str, str], run_date: str) -> str:
    raw_message = _sanitize_csv_text(message.get("message", ""))
    order_id = _sanitize_csv_text(message.get("order_id", ""))
    message_hash = _sanitize_csv_text(message.get("message_hash", "")) or _fingerprint_text(raw_message)
    source_id = _sanitize_csv_text(message.get("source_id", ""))
    if order_id:
        identity = f"{run_date}|order:{order_id}"
    else:
        identity = f"{run_date}|{message_hash}|{source_id}"
    return _fingerprint_text(identity)


def apply_daily_settlement_mode(
    messages: List[Dict[str, str]],
    config: Dict[str, Any],
    trace: TraceFn = None,
) -> Tuple[List[Dict[str, str]], bool]:
    """
    日结模式：
    - 首次运行做初始化基线，不入账；
    - 同一天重复运行时，同一消息 ID 不重复入账；
    - 支持仅处理最近 N 条消息，减少历史残留干扰。
    """
    enabled = _to_bool(config.get("daily_settlement_mode", False), False)
    if not enabled:
        return messages, False

    state_path = Path(str(config.get("daily_settlement_state_path", "data/daily_settlement_state.json")))
    retention_days = _to_int(config.get("daily_seen_retention_days", 30), 30, minimum=1)
    recent_limit = _to_int(config.get("daily_recent_message_limit", 3), 3, minimum=0)
    force_reinitialize = _to_bool(config.get("daily_force_reinitialize", False), False)

    today = _today_text()
    state = _load_daily_settlement_state(state_path)
    initialized = _to_bool(state.get("initialized", False), False) and not force_reinitialize

    daily_seen_raw = state.get("daily_seen_ids", {})
    if not isinstance(daily_seen_raw, dict):
        daily_seen_raw = {}

    # 清理过旧日期，防止状态文件无限增长
    keep_dates: Set[str] = set()
    now_date = _parse_datetime(today)
    if now_date is not None:
        for day_text in daily_seen_raw.keys():
            day_dt = _parse_datetime(str(day_text))
            if day_dt is None:
                continue
            if (now_date.date() - day_dt.date()).days <= retention_days:
                keep_dates.add(str(day_text))
    cleaned_daily_seen: Dict[str, List[str]] = {
        day: [str(x) for x in values if str(x).strip()]
        for day, values in daily_seen_raw.items()
        if day in keep_dates and isinstance(values, list)
    }

    all_ids_today = sorted({_build_daily_message_id(msg, today) for msg in messages})
    if not initialized:
        cleaned_daily_seen[today] = all_ids_today
        new_state = {
            "initialized": True,
            "initialized_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "init_message_count": len(messages),
            "daily_seen_ids": cleaned_daily_seen,
        }
        _save_daily_settlement_state(state_path, new_state)
        if trace:
            trace(
                f"[DAILY] 首次初始化完成: baseline_messages={len(messages)}, baseline_ids={len(all_ids_today)}，本次不入账。"
            )
        return [], True

    candidates = messages[-recent_limit:] if recent_limit > 0 else messages
    seen_today = set(str(x) for x in cleaned_daily_seen.get(today, []) if str(x).strip())
    selected: List[Dict[str, str]] = []
    for message in candidates:
        msg_id = _build_daily_message_id(message, today)
        if msg_id in seen_today:
            continue
        copied = dict(message)
        copied["daily_message_id"] = msg_id
        selected.append(copied)
        seen_today.add(msg_id)

    cleaned_daily_seen[today] = sorted(seen_today)
    updated_state = {
        "initialized": True,
        "initialized_at": state.get("initialized_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        "last_settlement_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "daily_seen_ids": cleaned_daily_seen,
    }
    _save_daily_settlement_state(state_path, updated_state)
    if trace:
        trace(
            f"[DAILY] 日结筛选: input={len(messages)}, candidates={len(candidates)}, output={len(selected)}, seen_today={len(seen_today)}"
        )
    return selected, False


def _resolve_message_epoch(message: Dict[str, str], fallback_epoch: int) -> int:
    parsed = _parse_datetime(_sanitize_csv_text(message.get("timestamp", "")))
    if parsed is None:
        return fallback_epoch
    try:
        return int(parsed.timestamp())
    except Exception:
        return fallback_epoch


def filter_messages_by_hash_time_window(
    messages: List[Dict[str, str]], config: Dict[str, Any], trace: TraceFn = None
) -> List[Dict[str, str]]:
    """仅按“同 hash + 时间窗口”过滤，避免同一笔被重复抓取。"""
    if not messages:
        return []

    start_marker = resolve_start_marker(config)
    order_id_digits = _to_int(config.get("order_id_digits", DEFAULT_ORDER_ID_DIGITS), DEFAULT_ORDER_ID_DIGITS, minimum=1)
    require_hash = _to_bool(
        config.get("order_id_require_hash", DEFAULT_ORDER_ID_REQUIRE_HASH),
        DEFAULT_ORDER_ID_REQUIRE_HASH,
    )
    marked_messages = _mark_messages(
        messages,
        start_marker=start_marker,
        order_id_digits=order_id_digits,
        require_hash=require_hash,
    )
    invalid_count = len(messages) - len(marked_messages)
    if trace and invalid_count > 0:
        trace(
            f"[MAIN] 消息格式过滤: 已忽略 {invalid_count} 条未携带有效单号的消息。"
        )
    if not marked_messages:
        return []

    enabled = _to_bool(config.get("message_hash_dedupe_enabled", True), True)
    if not enabled:
        if trace:
            trace("[MAIN] 已关闭消息hash时间窗过滤。")
        return marked_messages

    window_seconds = _to_int(config.get("message_hash_window_seconds", 120), 120, minimum=0)
    retention_seconds = _to_int(
        config.get("message_hash_state_retention_seconds", 7 * 24 * 3600),
        7 * 24 * 3600,
        minimum=0,
    )
    state_path = Path(str(config.get("message_hash_state_path", DEFAULT_MESSAGE_HASH_STATE_PATH)))

    state = _load_hash_state(state_path)
    now_epoch = int(datetime.now().timestamp())
    if retention_seconds > 0:
        min_epoch = now_epoch - retention_seconds
        state = {k: v for k, v in state.items() if v >= min_epoch}

    filtered: List[Dict[str, str]] = []
    skipped = 0
    marked = 0
    for message in marked_messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        if not raw_message:
            continue

        digest = _sanitize_csv_text(message.get("message_hash", "")) or _fingerprint_text(raw_message)
        msg_epoch = _resolve_message_epoch(message, now_epoch)
        last_seen = state.get(digest)
        if last_seen is not None and abs(msg_epoch - last_seen) <= window_seconds:
            skipped += 1
            continue

        state[digest] = msg_epoch
        copied = dict(message)
        copied["message"] = raw_message
        filtered.append(copied)
        marked += 1

    _save_hash_state(state_path, state)
    if trace:
        trace(
            f"[MAIN] 消息hash时间窗过滤(window={window_seconds}s): input={len(messages)}, output={len(filtered)}, marked={marked}, skipped={skipped}, state_size={len(state)}"
        )

    return filtered


def _normalize_input_messages(
    messages: List[Dict[str, str]],
    default_source_id: str = "local",
) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_default = _sanitize_csv_text(default_source_id) or "local"
    for message in messages:
        if not isinstance(message, dict):
            continue
        raw_message = _sanitize_csv_text(message.get("message", ""))
        if not raw_message:
            continue

        copied = dict(message)
        copied["message"] = raw_message
        copied["timestamp"] = _sanitize_csv_text(message.get("timestamp", "")) or now_text
        copied["source_id"] = _sanitize_csv_text(message.get("source_id", "")) or source_default

        message_hash = _sanitize_csv_text(message.get("message_hash", ""))
        if message_hash:
            copied["message_hash"] = message_hash

        captured_at = _sanitize_csv_text(message.get("message_captured_at", ""))
        if captured_at:
            copied["message_captured_at"] = captured_at

        normalized.append(copied)

    return normalized


def process_ledger_messages(
    raw_messages: List[Dict[str, str]],
    config: Dict[str, Any],
    trace: TraceFn = None,
    workflow: str = "capture",
) -> Dict[str, Any]:
    workflow_mode = str(workflow or "capture").strip().lower()
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config)
    data_path = Path(str(config.get("data_path", DEFAULT_DATA_PATH)))
    ensure_csv_file(data_path)

    normalized_raw = _normalize_input_messages(
        raw_messages,
        default_source_id="dify" if workflow_mode == "dify" else "capture",
    )

    if workflow_mode == "dify":
        apply_scope_filter = _to_bool(
            config.get("dify_apply_capture_scope", DEFAULT_DIFY_APPLY_CAPTURE_SCOPE),
            DEFAULT_DIFY_APPLY_CAPTURE_SCOPE,
        )
        daily_mode = _to_bool(
            config.get("dify_use_daily_settlement", DEFAULT_DIFY_USE_DAILY_SETTLEMENT),
            DEFAULT_DIFY_USE_DAILY_SETTLEMENT,
        )
        use_hash_window = _to_bool(
            config.get("dify_use_hash_time_window", DEFAULT_DIFY_USE_HASH_TIME_WINDOW),
            DEFAULT_DIFY_USE_HASH_TIME_WINDOW,
        )
    else:
        apply_scope_filter = True
        daily_mode = _to_bool(config.get("daily_settlement_mode", False), False)
        use_hash_window = True

    if apply_scope_filter:
        scoped_messages = filter_today_new_messages(normalized_raw, config, trace=trace)
    else:
        scoped_messages = normalized_raw
        if trace:
            trace(f"[MAIN] 已跳过capture_scope过滤(workflow={workflow_mode})")
    if trace:
        trace(f"[MAIN] 范围过滤后消息数={len(scoped_messages)}")

    order_id_digits = _to_int(config.get("order_id_digits", DEFAULT_ORDER_ID_DIGITS), DEFAULT_ORDER_ID_DIGITS, minimum=1)
    require_hash = _to_bool(
        config.get("order_id_require_hash", DEFAULT_ORDER_ID_REQUIRE_HASH),
        DEFAULT_ORDER_ID_REQUIRE_HASH,
    )

    initialized_now = False
    if daily_mode:
        marked_messages = _mark_messages(
            scoped_messages,
            start_marker=start_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
        )
        ignored_count = len(scoped_messages) - len(marked_messages)
        if ignored_count > 0 and trace:
            trace(f"[MAIN] 日结模式忽略未携带有效单号消息数={ignored_count}")
        messages_for_parse, initialized_now = apply_daily_settlement_mode(marked_messages, config, trace=trace)
        if trace:
            trace(f"[MAIN] 日结筛选后消息数={len(messages_for_parse)}")
    else:
        if use_hash_window:
            messages_for_parse = filter_messages_by_hash_time_window(scoped_messages, config, trace=trace)
            if trace:
                trace(f"[MAIN] hash时间窗过滤后消息数={len(messages_for_parse)}")
        else:
            messages_for_parse = _mark_messages(
                scoped_messages,
                start_marker=start_marker,
                order_id_digits=order_id_digits,
                require_hash=require_hash,
            )
            if trace:
                trace(f"[MAIN] 已跳过hash时间窗过滤(workflow={workflow_mode}), marked={len(messages_for_parse)}")

    known_store_lookup = build_known_store_lookup(
        config,
        enabled_key="known_store_lookup_enabled",
        path_key="known_store_lookup_path",
        default_enabled=DEFAULT_KNOWN_STORE_LOOKUP_ENABLED,
        default_path=DEFAULT_KNOWN_STORE_LOOKUP_PATH,
        trace=trace,
    )
    known_store_hits = 0

    if initialized_now:
        return {
            "status": "initialized",
            "workflow": workflow_mode,
            "initialized_now": True,
            "start_marker": start_marker,
            "end_marker": end_marker,
            "input_messages_count": len(normalized_raw),
            "scoped_messages_count": len(scoped_messages),
            "messages_for_parse_count": len(messages_for_parse),
            "parsed_records_count": 0,
            "unique_records_count": 0,
            "written_records_count": 0,
            "json_output_enabled": _to_bool(config.get("json_output_enabled", True), True),
            "json_written_bills": 0,
            "data_path": str(data_path),
            "json_path": str(config.get("json_output_path", DEFAULT_JSON_OUTPUT_PATH)),
            "known_store_lookup_enabled": bool(known_store_lookup.get("enabled", False)),
            "known_store_hits": 0,
            "records": [],
            "bills": [],
        }

    parsed_records = parse_messages(messages_for_parse, start_marker, end_marker=end_marker)
    known_store_hits = annotate_records_with_known_stores(
        parsed_records,
        known_store_lookup,
        max_matches=_to_int(
            config.get("known_store_lookup_max_matches", DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES),
            DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES,
            minimum=1,
        ),
    )
    if trace:
        trace(f"[MAIN] 解析后记录数={len(parsed_records)}")
        if known_store_lookup.get("enabled", False):
            trace(
                f"[MAIN] 店铺直查命中记录数={known_store_hits}, stores={known_store_lookup.get('stores_count', 0)}"
            )

    deduplicate_within_run = _to_bool(config.get("deduplicate_within_run", True), True)
    if deduplicate_within_run:
        unique_records = deduplicate_records(parsed_records)
        if trace:
            trace(f"[MAIN] 单次去重后记录数={len(unique_records)}")
    else:
        unique_records = parsed_records
        if trace:
            trace(f"[MAIN] 已关闭单次去重，记录数={len(unique_records)}")

    written_records = write_records(data_path, unique_records)
    if trace:
        trace(f"[MAIN] 数据已写入={data_path}, written_records={written_records}")

    json_output_enabled = _to_bool(config.get("json_output_enabled", True), True)
    bills: List[Dict[str, Any]] = []
    json_path: Optional[Path] = None
    json_written_bills = 0
    if json_output_enabled:
        json_path = Path(str(config.get("json_output_path", DEFAULT_JSON_OUTPUT_PATH)))
        append_history = _to_bool(config.get("json_output_append_history", True), True)
        bills = build_bill_payloads(unique_records)
        json_written_bills = write_bill_json_output(json_path, bills, append_history=append_history)
        if trace:
            trace(f"[MAIN] JSON已写入={json_path}, bills={json_written_bills}")

    return {
        "status": "ok",
        "workflow": workflow_mode,
        "initialized_now": False,
        "start_marker": start_marker,
        "end_marker": end_marker,
        "input_messages_count": len(normalized_raw),
        "scoped_messages_count": len(scoped_messages),
        "messages_for_parse_count": len(messages_for_parse),
        "parsed_records_count": len(parsed_records),
        "unique_records_count": len(unique_records),
        "written_records_count": written_records,
        "json_output_enabled": json_output_enabled,
        "json_written_bills": json_written_bills,
        "data_path": str(data_path),
        "json_path": str(json_path) if json_path is not None else "",
        "known_store_lookup_enabled": bool(known_store_lookup.get("enabled", False)),
        "known_store_hits": known_store_hits,
        "records": unique_records,
        "bills": bills,
    }


def write_records(csv_path: Path, records: List[Dict[str, Any]]) -> int:
    """将记录追加写入 CSV，返回写入条数。"""
    if not records:
        return 0

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with csv_path.open("a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        for record in records:
            writer.writerow(
                {
                    "timestamp": _sanitize_csv_text(record.get("timestamp", "")),
                    "order_id": _sanitize_csv_text(record.get("order_id", "")),
                    "item": _sanitize_csv_text(record.get("item", "")),
                    "amount": record.get("amount", 0.0),
                    "raw_message": _sanitize_csv_text(record.get("raw_message", "")),
                    "message_hash": _sanitize_csv_text(record.get("message_hash", "")),
                    "message_captured_at": _sanitize_csv_text(record.get("message_captured_at", "")),
                    "recorded_at": now_text,
                }
            )

    return len(records)


def build_bill_payloads(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bills: Dict[str, Dict[str, Any]] = {}
    for record in records:
        order_id = _sanitize_csv_text(record.get("order_id", ""))
        if not order_id:
            continue

        message_hash = _sanitize_csv_text(record.get("message_hash", ""))
        source_id = _sanitize_csv_text(record.get("source_id", ""))
        bill_key = f"{order_id}|{message_hash}|{source_id}"
        bill = bills.get(bill_key)
        if bill is None:
            bill = {
                "order_id": order_id,
                "name": _sanitize_csv_text(record.get("name", "")),
                "message_hash": message_hash,
                "message_captured_at": _sanitize_csv_text(record.get("message_captured_at", "")),
                "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "source_id": source_id,
                "timestamp": _sanitize_csv_text(record.get("timestamp", "")),
                "raw_message": _sanitize_csv_text(record.get("raw_message", "")),
                "items": [],
            }
            bills[bill_key] = bill
        elif not _sanitize_csv_text(bill.get("name", "")):
            bill["name"] = _sanitize_csv_text(record.get("name", ""))

        bill["items"].append(
            {
                "item": _sanitize_csv_text(record.get("item", "")),
                "amount": float(record.get("amount", 0.0)),
            }
        )

    payloads: List[Dict[str, Any]] = []
    for bill in bills.values():
        total_amount = sum(float(x.get("amount", 0.0)) for x in bill["items"])
        bill["item_count"] = len(bill["items"])
        bill["total_amount"] = total_amount
        payloads.append(bill)
    return payloads


def write_bill_json_output(
    json_path: Path,
    bills: List[Dict[str, Any]],
    append_history: bool = True,
) -> int:
    if not bills:
        return 0

    json_path.parent.mkdir(parents=True, exist_ok=True)
    existing: List[Dict[str, Any]] = []
    if append_history and json_path.exists():
        try:
            with json_path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
            if isinstance(payload, dict) and isinstance(payload.get("bills"), list):
                existing = [x for x in payload.get("bills", []) if isinstance(x, dict)]
            elif isinstance(payload, list):
                existing = [x for x in payload if isinstance(x, dict)]
        except Exception:
            existing = []

    merged = existing + bills
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "bills": merged,
    }
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    return len(bills)


def _build_manual_messages(start_marker: str) -> List[Dict[str, str]]:
    """抓取失败时允许手动粘贴一条记账消息。"""
    print(
        f"[手动模式] 可粘贴一整条记账消息（首行必须类似 #{start_marker}{DEFAULT_MANUAL_ORDER_ID_SAMPLE}），"
        f"输入单独一行 {DEFAULT_MANUAL_END_TOKEN} 结束："
    )
    lines: List[str] = []

    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == DEFAULT_MANUAL_END_TOKEN:
            break
        lines.append(line)

    text = "\n".join(lines).strip()
    if not text:
        return []

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return [{"timestamp": ts, "message": text}]


def _fetch_messages_auto(config: Dict[str, Any], trace: TraceFn = None) -> List[Dict[str, str]]:
    """自动抓取消息：支持 win32_memory / win32_clipboard / uia。"""
    backend = str(config.get("capture_backend", DEFAULT_CAPTURE_BACKEND)).strip().lower()
    errors: List[str] = []

    def _try_win32_memory() -> Optional[List[Dict[str, str]]]:
        try:
            if trace:
                trace("[AUTO] 开始尝试 WIN32_MEMORY 后端")
            messages = fetch_recent_messages_win32_memory(config, trace=trace)
            if trace:
                trace(f"[AUTO] WIN32_MEMORY 后端返回消息数={len(messages)}")
            return messages
        except Win32MemoryMonitorError as exc:
            errors.append(f"win32_memory: {exc}")
            if trace:
                trace(f"[AUTO] WIN32_MEMORY 后端失败: {exc}")
            return None

    def _try_win32() -> Optional[List[Dict[str, str]]]:
        try:
            if trace:
                trace("[AUTO] 开始尝试 WIN32_CLIPBOARD 后端")
            messages = fetch_recent_messages_win32_clipboard(config, trace=trace)
            if trace:
                trace(f"[AUTO] WIN32_CLIPBOARD 后端返回消息数={len(messages)}")
            return messages
        except Win32MonitorError as exc:
            errors.append(f"win32_clipboard: {exc}")
            if trace:
                trace(f"[AUTO] WIN32_CLIPBOARD 后端失败: {exc}")
            return None

    def _try_uia() -> Optional[List[Dict[str, str]]]:
        try:
            if trace:
                trace("[AUTO] 开始尝试 UIA 后端")
            messages = fetch_recent_messages(config, trace=trace)
            if trace:
                trace(f"[AUTO] UIA 后端返回消息数={len(messages)}")
            return messages
        except MonitorError as exc:
            errors.append(f"uia: {exc}")
            if trace:
                trace(f"[AUTO] UIA 后端失败: {exc}")
            return None

    if backend in {"win32_memory", "memory", "win32mem", "mem"}:
        messages = _try_win32_memory()
        if messages is not None:
            return messages
    elif backend in {"win32", "win32_clipboard", "clipboard"}:
        messages = _try_win32()
        if messages is not None:
            return messages
    elif backend == "uia":
        messages = _try_uia()
        if messages is not None:
            return messages
    else:
        messages = _try_win32_memory()
        if messages is not None:
            return messages
        messages = _try_win32()
        if messages is not None:
            return messages
        messages = _try_uia()
        if messages is not None:
            return messages

    reason = "；".join(errors) if errors else "未知错误"
    raise MonitorError(f"自动抓取失败：{reason}")


def print_run_result(records: List[Dict[str, Any]], csv_path: Path) -> None:
    """打印本次执行结果。"""
    if not records:
        print("[结果] 本次未发现可入账的新消息。")
        return

    print(f"[结果] 本次成功写入 {len(records)} 条记录 -> {csv_path}")
    count_total = sum(
        float(record.get("amount", 0.0))
        for record in records
        if str(record.get("record_type", "")) == "count"
    )
    for idx, record in enumerate(records, start=1):
        if str(record.get("record_type", "")) == "count":
            value_text = f"数量: {record.get('amount', 0.0):.2f}"
        else:
            value_text = f"金额: {record.get('amount', 0.0):.2f} 元"

        print(
            f"  {idx}. 单号: {record.get('order_id', '-') or '-'} | 时间: {record.get('timestamp', '-') or '-'} | 项目: {record.get('item', '')} | {value_text}"
        )

    if count_total > 0:
        print(f"[统计] 本次数量合计: {count_total:.2f}")


def main() -> None:
    """程序入口。"""
    print("[启动] ZongziLedger 本地记账开始执行。")

    if not check_environment():
        return

    try:
        config, dotenv_applied = load_runtime_config(DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH)
    except Exception as exc:
        print(f"[错误] 读取配置失败：{exc}")
        return

    logger, log_path = init_run_logger(config)
    trace = _make_trace(logger)
    trace(f"[MAIN] 启动时间={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    trace(f"[MAIN] Python={sys.version}")
    trace(f"[MAIN] 管理员权限={'是' if _is_admin() else '否'}")
    trace(
        f"[MAIN] 配置摘要: capture_mode={config.get('capture_mode', DEFAULT_CAPTURE_MODE)}, "
        f"prefix={config.get('prefix', DEFAULT_PREFIX)}, max_messages={config.get('max_messages', DEFAULT_MAX_MESSAGES)}"
    )
    if dotenv_applied > 0:
        trace(f"[MAIN] 已加载 .env 覆盖项数量={dotenv_applied}")
    print(f"[日志] 抓取日志文件：{log_path}")

    start_marker = resolve_start_marker(config)
    capture_mode = str(config.get("capture_mode", DEFAULT_CAPTURE_MODE)).strip().lower()
    raw_messages: List[Dict[str, str]]

    if capture_mode == "manual":
        print("[模式] 当前为手动录入模式（capture_mode=manual）。")
        raw_messages = _build_manual_messages(start_marker)
        if not raw_messages:
            print("[提示] 未收到手动输入，已结束。")
            return
    else:
        try:
            raw_messages = _fetch_messages_auto(config, trace=trace)
            try:
                inspected_count = int(config.get("max_messages", DEFAULT_MAX_MESSAGES))
            except (TypeError, ValueError):
                inspected_count = DEFAULT_MAX_MESSAGES
            if inspected_count <= 0:
                inspected_count = DEFAULT_MAX_MESSAGES
            print(
                f"[抓取] 已检查窗口最近 {inspected_count} 条消息，前缀 {start_marker} 命中 {len(raw_messages)} 条。"
            )
        except MonitorError as exc:
            trace(f"[MAIN] 自动抓取失败: {exc}")
            print(f"[提示] 抓取消息失败：{exc}")
            print(f"[提示] 详细过程见日志：{log_path}")
            return
        except Exception as exc:
            trace(f"[MAIN] 未预期异常: {exc}")
            trace(traceback.format_exc())
            print(f"[错误] 抓取过程发生未预期异常：{exc}")
            print(f"[提示] 详细过程见日志：{log_path}")
            return

    result = process_ledger_messages(raw_messages, config, trace=trace, workflow="capture")
    data_path = Path(str(result.get("data_path") or config.get("data_path", DEFAULT_DATA_PATH)))

    if _to_bool(result.get("initialized_now", False), False):
        print("[日结初始化] 已建立历史基线。本次不写入账单，请下次运行开始正式结算。")
        print(f"[提示] 详细过程见日志：{log_path}")
        return

    unique_records = list(result.get("records", []))
    print_run_result(unique_records, data_path)

    json_output_enabled = _to_bool(result.get("json_output_enabled", False), False)
    if json_output_enabled:
        json_path_text = str(result.get("json_path", "") or "")
        if json_path_text:
            print(f"[JSON] 本次输出 {int(result.get('json_written_bills', 0))} 笔账单 -> {json_path_text}")

    show_totals = _to_bool(config.get("show_item_totals", True), True)
    if show_totals:
        top_n = _to_int(config.get("item_totals_top_n", 20), 20, minimum=0)
        totals = summarize_item_totals(data_path, trace=trace)
        print_item_totals(totals, top_n=top_n)


if __name__ == "__main__":
    main()
