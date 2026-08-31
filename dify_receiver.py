from __future__ import annotations

import json
import re
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.contracts import ensure_normalized_message_contract
from core.constants import (
    DEFAULT_DIFY_API_TOKEN,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DATA_PATH,
    DEFAULT_DOTENV_PATH,
    DEFAULT_JSON_OUTPUT_PATH,
    DEFAULT_KNOWN_STORE_LOOKUP_ENABLED,
    DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES,
    DEFAULT_KNOWN_STORE_LOOKUP_PATH,
    DEFAULT_ORDER_ID_DIGITS,
    DEFAULT_ORDER_ID_REQUIRE_HASH,
    resolve_end_marker,
    resolve_start_marker,
)
from core.security import normalize_loopback_bind_host, request_is_authorized
from core.parser import parse_ledger_message_multi
from core.store_lookup import annotate_records_with_known_stores, build_known_store_lookup, lookup_known_stores
from main import (
    _extract_order_id_from_message,
    _fingerprint_text,
    _is_admin,
    _make_trace,
    _mark_messages,
    _sanitize_csv_text,
    _to_bool,
    _to_int,
    apply_daily_settlement_mode,
    apply_dotenv_overrides,
    build_bill_payloads,
    deduplicate_records,
    ensure_csv_file,
    filter_messages_by_hash_time_window,
    filter_today_new_messages,
    init_run_logger,
    load_config,
    load_dotenv,
    write_bill_json_output,
    write_records,
)
from setup import check_environment

TraceFn = Optional[Callable[[str], None]]

DEFAULT_DIFY_LISTEN_HOST = "127.0.0.1"
DEFAULT_DIFY_LISTEN_PORT = 18888
DEFAULT_DIFY_INGEST_PATH = "/api/dify/ingest"
DEFAULT_DIFY_HEALTH_PATH = "/api/health"
DEFAULT_DIFY_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
DEFAULT_DIFY_SOURCE_ID = "dify"
DEFAULT_REVIEW_QUEUE_PATH = "data/review_queue.jsonl"
DEFAULT_REVIEW_QUEUE_FALLBACK_PATH = "logs/review_queue_fallback.jsonl"

PAYLOAD_SCHEMA_VERSION = "zongziledger-dify-clean-v1"
MESSAGE_TEXT_KEYS: Tuple[str, ...] = (
    "message",
    "normalized_block",
    "cleaned_message",
    "raw_message",
    "content",
    "text",
    "body",
)
TIMESTAMP_KEYS: Tuple[str, ...] = ("timestamp", "message_time", "created_at", "event_time", "time")
SOURCE_KEYS: Tuple[str, ...] = ("source_id", "chat_name", "chat_id", "conversation_id", "source")
MESSAGE_LIST_KEYS: Tuple[str, ...] = (
    "messages",
    "normalized_messages",
    "normalized_block",
    "normalized_blocks",
    "ledger_messages",
    "candidate_messages",
    "candidate_results",
    "records",
    "items",
)
CANDIDATE_FLAG_KEYS: Tuple[str, ...] = ("is_candidate", "candidate", "should_clean", "need_cleaning")
ORDER_HEADER_LINE_RE = re.compile(r"^[#\uFF03]?\s*.+?\d{8}\s*$")
ORDER_HEADER_FUZZY_RE = re.compile(r"[#\uFF03]?\s*([0-9]{8})")
ITEM_LINE_RE = re.compile(r".*?\d+(?:\.\d+)?\s*$")


def _trace(trace: TraceFn, message: str) -> None:
    if trace is None:
        return
    try:
        trace(message)
    except Exception:
        pass


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _compact_text(value: Any, max_len: int = 240) -> str:
    text = _sanitize_csv_text(value)
    if len(text) <= max_len:
        return text
    return f"{text[:max_len]}..."


def _pick_text_from_map(payload: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = _sanitize_csv_text(value)
            if text:
                return text
            continue
        if isinstance(value, (int, float, bool)):
            return _sanitize_csv_text(str(value))
        if isinstance(value, list):
            lines = [_sanitize_csv_text(x) for x in value if _sanitize_csv_text(x)]
            if lines:
                return "\n".join(lines)
    return ""


def _extract_candidate_entries(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        return [payload]
    if not isinstance(payload, dict):
        return []

    for key in MESSAGE_LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, list):
            return value

    for key in ("outputs", "data", "result", "payload"):
        nested = payload.get(key)
        nested_entries = _extract_candidate_entries(nested)
        if nested_entries:
            return nested_entries

    if _pick_text_from_map(payload, MESSAGE_TEXT_KEYS):
        return [payload]
    return []


def _is_candidate_entry(entry: Dict[str, Any]) -> bool:
    for key in CANDIDATE_FLAG_KEYS:
        if key not in entry:
            continue
        return _to_bool(entry.get(key), False)
    return True


def _build_review_item(
    stage: str,
    reason: str,
    raw_message: str = "",
    *,
    source_id: str = "",
    timestamp: str = "",
    message_hash: str = "",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "queued_at": _now_text(),
        "stage": _sanitize_csv_text(stage),
        "reason": _sanitize_csv_text(reason),
        "raw_message": _sanitize_csv_text(raw_message),
        "source_id": _sanitize_csv_text(source_id),
        "timestamp": _sanitize_csv_text(timestamp),
        "message_hash": _sanitize_csv_text(message_hash),
    }
    if extra:
        item["extra"] = _to_json_safe(extra)
    return item


def append_review_queue(
    queue_path: Path,
    items: Sequence[Dict[str, Any]],
    trace: TraceFn = None,
) -> int:
    if not items:
        return 0

    def _write(path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with path.open("a", encoding="utf-8") as f:
            for item in items:
                payload = _to_json_safe(item)
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
                count += 1
        return count

    try:
        wrote = _write(queue_path)
        _trace(trace, f"[DIFY] 已写入复核队列: path={queue_path}, count={wrote}")
        return wrote
    except Exception as exc:
        fallback_path = Path(DEFAULT_REVIEW_QUEUE_FALLBACK_PATH)
        _trace(trace, f"[DIFY] 复核队列写入失败，切换fallback: path={queue_path}, error={exc}")
        try:
            wrote = _write(fallback_path)
            _trace(trace, f"[DIFY] 已写入复核队列fallback: path={fallback_path}, count={wrote}")
            return wrote
        except Exception as fallback_exc:
            _trace(trace, f"[DIFY] 复核队列fallback仍失败: error={fallback_exc}")
            return 0


def _extract_extra_context(entry: Dict[str, Any]) -> Dict[str, Any]:
    keep_keys = {
        "message",
        "normalized_block",
        "cleaned_message",
        "raw_message",
        "content",
        "text",
        "body",
        "timestamp",
        "source_id",
        "message_hash",
        "message_captured_at",
        "name",
        "raw_context",
        "normalize_hint",
    }
    output: Dict[str, Any] = {}
    for key, value in entry.items():
        if key in keep_keys:
            continue
        output[str(key)] = _to_json_safe(value)
    return output


def _is_end_marker_line(line: str, end_marker: str) -> bool:
    text = _sanitize_csv_text(line).strip().strip("。.!！?？；;:：,，")
    return bool(text) and text == end_marker


def _normalize_header_line(line: str, start_marker: str, order_id_digits: int) -> str:
    text = _sanitize_csv_text(line).strip().strip("。.!！?？；;:：,，")
    if not text:
        return ""
    if not ORDER_HEADER_LINE_RE.fullmatch(text):
        return ""

    marker_pos = text.find(start_marker)
    if marker_pos >= 0:
        tail = text[marker_pos + len(start_marker) :]
    else:
        tail = text
    match = ORDER_HEADER_FUZZY_RE.search(tail)
    if not match:
        return ""
    order_id = _sanitize_csv_text(match.group(1))
    if len(order_id) != max(1, int(order_id_digits)):
        return ""
    return f"#{start_marker}{order_id}"


def _looks_like_ledger_message(text: str) -> bool:
    normalized = _sanitize_csv_text(text)
    if not normalized:
        return False
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) < 2:
        return False
    if ORDER_HEADER_LINE_RE.fullmatch(lines[0]):
        item_hits = sum(1 for line in lines[1:] if ITEM_LINE_RE.fullmatch(line))
        return item_hits > 0

    # Plain-text fallback: allow "name(optional) + item lines".
    item_hits_all = sum(1 for line in lines if ITEM_LINE_RE.fullmatch(line))
    if item_hits_all > 0:
        return True
    item_hits_payload = sum(1 for line in lines[1:] if ITEM_LINE_RE.fullmatch(line))
    return item_hits_payload > 0


def _classify_normalize_hint(message_text: str, start_marker: str, end_marker: str) -> str:
    normalized = _sanitize_csv_text(message_text)
    if not normalized:
        return "fallback_sample"
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if not lines:
        return "fallback_sample"

    has_start = any(start_marker in line for line in lines)
    has_end = any(_is_end_marker_line(line, end_marker) for line in lines)
    if has_start and has_end and _looks_like_ledger_message(normalized):
        return "structured_candidate"
    if has_start or _looks_like_ledger_message(normalized):
        return "salvage_candidate"
    return "fallback_sample"


def _normalize_message_block(
    raw_message: str,
    start_marker: str,
    end_marker: str,
    order_id_digits: int,
    require_hash: bool,
) -> Tuple[str, str]:
    text = _sanitize_csv_text(raw_message)
    if not text:
        return "", "empty_message"

    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return "", "empty_message"

    header_idx = -1
    normalized_header = ""
    for idx, line in enumerate(lines):
        normalized_line = _normalize_header_line(line, start_marker=start_marker, order_id_digits=order_id_digits)
        if normalized_line:
            header_idx = idx
            normalized_header = normalized_line
            break
    if header_idx < 0:
        if _looks_like_ledger_message(text):
            return "\n".join(lines).strip(), "plain_text_no_header"
        return "", "no_order_header"

    candidate_lines = lines[header_idx:]
    candidate_lines[0] = normalized_header
    end_idx = -1
    for idx, line in enumerate(candidate_lines[1:], start=1):
        if _is_end_marker_line(line, end_marker):
            end_idx = idx
            break
    if end_idx >= 0:
        candidate_lines = candidate_lines[: end_idx + 1]
        candidate_lines[-1] = end_marker
    else:
        candidate_lines.append(end_marker)

    if not candidate_lines:
        return "", "empty_after_header"
    if len(candidate_lines) < 3:
        return "", "too_short_after_normalize"

    normalized = "\n".join(candidate_lines).strip()
    order_id = _extract_order_id_from_message(
        normalized,
        start_marker=start_marker,
        order_id_digits=order_id_digits,
        require_hash=require_hash,
    )
    if not order_id:
        return "", "invalid_order_header_after_normalize"
    return normalized, "normalized"


def _normalize_messages_for_ledger(
    messages: List[Dict[str, Any]],
    *,
    start_marker: str,
    end_marker: str,
    order_id_digits: int,
    require_hash: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    normalized_messages: List[Dict[str, Any]] = []
    unstructured_items: List[Dict[str, Any]] = []
    salvage_count = 0
    structured_count = 0

    for message in messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        if not raw_message:
            unstructured_items.append(
                _build_review_item(
                    "normalize",
                    "empty_message",
                    "",
                    source_id=_sanitize_csv_text(message.get("source_id", "")),
                    timestamp=_sanitize_csv_text(message.get("timestamp", "")),
                    message_hash=_sanitize_csv_text(message.get("message_hash", "")),
                )
            )
            continue

        normalized_block, reason = _normalize_message_block(
            raw_message,
            start_marker=start_marker,
            end_marker=end_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
        )
        if not normalized_block:
            unstructured_items.append(
                _build_review_item(
                    "normalize",
                    reason,
                    raw_message,
                    source_id=_sanitize_csv_text(message.get("source_id", "")),
                    timestamp=_sanitize_csv_text(message.get("timestamp", "")),
                    message_hash=_sanitize_csv_text(message.get("message_hash", "")),
                    extra={"normalize_hint": _sanitize_csv_text(message.get("normalize_hint", ""))},
                )
            )
            continue

        copied = dict(message)
        copied["message"] = normalized_block
        copied["raw_message_original"] = raw_message
        if normalized_block != raw_message:
            salvage_count += 1
            copied["normalized_by"] = "local_receiver"
        else:
            structured_count += 1
        normalized_messages.append(copied)

    stats = {
        "input_messages": len(messages),
        "normalized_messages": len(normalized_messages),
        "structured_count": structured_count,
        "salvage_count": salvage_count,
        "unstructured_count": len(unstructured_items),
    }
    return normalized_messages, unstructured_items, stats


def normalize_dify_payload(
    payload: Any,
    *,
    default_source_id: str = DEFAULT_DIFY_SOURCE_ID,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    entries = _extract_candidate_entries(payload)
    now_text = _now_text()

    source_from_payload = ""
    capture_meta: Dict[str, Any] = {}
    top_level_raw_context: Dict[str, Any] = {}
    start_marker = resolve_start_marker({})
    end_marker = resolve_end_marker({})
    if isinstance(payload, dict):
        source_from_payload = _pick_text_from_map(payload, SOURCE_KEYS)
        if not source_from_payload and isinstance(payload.get("meta"), dict):
            source_from_payload = _pick_text_from_map(payload.get("meta", {}), SOURCE_KEYS)
        if isinstance(payload.get("capture_meta"), dict):
            capture_meta = dict(payload.get("capture_meta", {}))
            meta_start = _sanitize_csv_text(capture_meta.get("start_marker", ""))
            meta_end = _sanitize_csv_text(capture_meta.get("end_marker", ""))
            if meta_start:
                start_marker = meta_start.lstrip("#")
            if meta_end:
                end_marker = meta_end
        if isinstance(payload.get("normalization_context"), dict):
            top_level_raw_context["normalization_context"] = _to_json_safe(payload.get("normalization_context"))
        top_level_raw_context["schema_version"] = _sanitize_csv_text(payload.get("schema_version", ""))

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    if not entries:
        rejected.append(
            _build_review_item(
                "ingest",
                "no_message_entries",
                extra={"payload_type": type(payload).__name__},
            )
        )
        return accepted, rejected

    for idx, entry in enumerate(entries):
        message_text = ""
        entry_map: Dict[str, Any] = {}

        if isinstance(entry, str):
            message_text = entry
        elif isinstance(entry, dict):
            entry_map = entry
            if not _is_candidate_entry(entry_map):
                continue
            message_text = _pick_text_from_map(entry, MESSAGE_TEXT_KEYS)
        else:
            rejected.append(
                _build_review_item(
                    "ingest",
                    "invalid_entry_type",
                    extra={"entry_index": idx, "entry_type": type(entry).__name__},
                )
            )
            continue

        raw_message = _sanitize_csv_text(message_text)
        if not raw_message:
            rejected.append(
                _build_review_item(
                    "ingest",
                    "empty_message",
                    extra={
                        "entry_index": idx,
                        "entry_preview": _compact_text(entry),
                    },
                )
            )
            continue

        timestamp = _sanitize_csv_text(_pick_text_from_map(entry_map, TIMESTAMP_KEYS)) or now_text
        source_id = (
            _sanitize_csv_text(_pick_text_from_map(entry_map, SOURCE_KEYS))
            or _sanitize_csv_text(source_from_payload)
            or default_source_id
        )
        message_hash = _sanitize_csv_text(_pick_text_from_map(entry_map, ("message_hash", "hash")))
        message_captured_at = _sanitize_csv_text(
            _pick_text_from_map(entry_map, ("message_captured_at", "captured_at"))
        )
        normalize_hint = _sanitize_csv_text(_pick_text_from_map(entry_map, ("normalize_hint", "cleaning_hint")))
        if not normalize_hint:
            normalize_hint = _classify_normalize_hint(raw_message, start_marker=start_marker, end_marker=end_marker)

        merged_context = dict(top_level_raw_context)
        merged_context.update(_extract_extra_context(entry_map))
        if isinstance(entry_map.get("raw_context"), dict):
            merged_context["raw_context"] = _to_json_safe(entry_map.get("raw_context"))
        if capture_meta:
            merged_context["capture_meta"] = _to_json_safe(capture_meta)
        if isinstance(entry_map.get("name"), str):
            name_text = _sanitize_csv_text(entry_map.get("name", ""))
            if name_text:
                merged_context["name"] = name_text

        accepted.append(
            {
                "timestamp": timestamp,
                "message": raw_message,
                "raw_message": raw_message,
                "source_id": source_id,
                "message_hash": message_hash or _fingerprint_text(raw_message),
                "message_captured_at": message_captured_at or now_text,
                "normalize_hint": normalize_hint,
                "raw_context": merged_context,
            }
        )

    contracted_messages, contract_issues, _contract_stats = ensure_normalized_message_contract(
        accepted,
        default_source_id=default_source_id,
        source="dify_receiver.normalize",
        now_text=now_text,
    )
    for issue in contract_issues:
        reason_text = _sanitize_csv_text(issue.get("reason", "")) or "contract_issue"
        rejected.append(
            _build_review_item(
                "contract",
                reason_text,
                _sanitize_csv_text(issue.get("entry_preview", "")),
                source_id=_sanitize_csv_text(issue.get("source_id", "")),
                timestamp=_sanitize_csv_text(issue.get("timestamp", "")),
                message_hash=_sanitize_csv_text(issue.get("message_hash", "")),
                extra={"contract_issue": _to_json_safe(issue)},
            )
        )

    return contracted_messages, rejected


def _parse_messages_with_failures(
    messages: List[Dict[str, Any]],
    start_marker: str,
    end_marker: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for message in messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        original_raw = _sanitize_csv_text(message.get("raw_message_original", "")) or raw_message
        parsed = parse_ledger_message_multi(raw_message, start_marker, end_marker=end_marker)
        if not parsed:
            failures.append(
                _build_review_item(
                    "parse",
                    "parse_failed",
                    original_raw,
                    source_id=_sanitize_csv_text(message.get("source_id", "")),
                    timestamp=_sanitize_csv_text(message.get("timestamp", "")),
                    message_hash=_sanitize_csv_text(message.get("message_hash", "")),
                    extra={
                        "normalized_message": raw_message,
                        "normalize_hint": _sanitize_csv_text(message.get("normalize_hint", "")),
                    },
                )
            )
            continue

        timestamp = _sanitize_csv_text(message.get("timestamp", ""))
        source_id = _sanitize_csv_text(message.get("source_id", ""))
        message_hash = _sanitize_csv_text(message.get("message_hash", ""))
        message_captured_at = _sanitize_csv_text(message.get("message_captured_at", ""))
        for item in parsed:
            copied = dict(item)
            copied["timestamp"] = timestamp
            copied["source_id"] = source_id
            copied["message_hash"] = message_hash
            copied["message_captured_at"] = message_captured_at
            records.append(copied)

    return records, failures


def _extract_store_name_from_raw_message(raw_message: str) -> str:
    text = _sanitize_csv_text(raw_message)
    if not text:
        return ""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return ""
    payload_lines = list(lines)
    header = lines[0].lstrip("#＃").strip()
    if re.fullmatch(r"记账\s*\d{0,8}", header):
        payload_lines = lines[1:]
    if payload_lines and payload_lines[-1] == "结束":
        payload_lines = payload_lines[:-1]
    if not payload_lines:
        return ""
    idx = 0
    while idx < len(payload_lines):
        first_payload = _sanitize_csv_text(payload_lines[idx]).rstrip("：:")
        if not first_payload:
            idx += 1
            continue
        marker_like = first_payload.lstrip("#＃").strip()
        if re.fullmatch(r"记账\s*\d{0,8}", marker_like):
            idx += 1
            continue
        if first_payload in {"#记账", "记账", "#"}:
            idx += 1
            continue
        # 明细行/规格头不作为店名；继续向后找真正店名行。
        from core import parser as _parser  # local import to avoid circular import at module init

        if _parser._as_volume_header(first_payload) or _parser._parse_count_line(first_payload) is not None:
            idx += 1
            continue
        return first_payload
    return ""


def _enforce_known_store_gate(
    records: List[Dict[str, Any]],
    lookup: Dict[str, Any],
    *,
    enabled: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    if not records or not enabled:
        return records, [], 0

    accepted: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    rejected_count = 0
    for record in records:
        raw_message = _sanitize_csv_text(record.get("raw_message", ""))
        source_id = _sanitize_csv_text(record.get("source_id", ""))
        timestamp = _sanitize_csv_text(record.get("timestamp", ""))
        message_hash = _sanitize_csv_text(record.get("message_hash", ""))
        store_name = _extract_store_name_from_raw_message(raw_message)
        if not store_name:
            rejected_count += 1
            rejected.append(
                _build_review_item(
                    "store_gate",
                    "missing_store_line",
                    raw_message,
                    source_id=source_id,
                    timestamp=timestamp,
                    message_hash=message_hash,
                    extra={
                        "item": _sanitize_csv_text(record.get("item", "")),
                    },
                )
            )
            continue
        matches = lookup_known_stores(store_name, lookup, max_matches=3)
        if not matches:
            rejected_count += 1
            rejected.append(
                _build_review_item(
                    "store_gate",
                    "unknown_store",
                    raw_message,
                    source_id=source_id,
                    timestamp=timestamp,
                    message_hash=message_hash,
                    extra={
                        "store_name": store_name,
                        "item": _sanitize_csv_text(record.get("item", "")),
                    },
                )
            )
            continue
        copied = dict(record)
        copied["known_store"] = matches[0]
        copied["known_store_candidates"] = matches
        accepted.append(copied)

    return accepted, rejected, rejected_count


def process_dify_messages(
    messages: List[Dict[str, Any]],
    config: Dict[str, Any],
    trace: TraceFn = None,
) -> Dict[str, Any]:
    data_path = Path(str(config.get("data_path", DEFAULT_DATA_PATH)))
    ensure_csv_file(data_path)
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config)

    scoped_messages = filter_today_new_messages(messages, config, trace=trace)
    order_id_digits = _to_int(config.get("order_id_digits", DEFAULT_ORDER_ID_DIGITS), DEFAULT_ORDER_ID_DIGITS, minimum=1)
    require_hash = _to_bool(
        config.get("order_id_require_hash", DEFAULT_ORDER_ID_REQUIRE_HASH),
        DEFAULT_ORDER_ID_REQUIRE_HASH,
    )
    normalized_messages, normalize_unstructured, normalize_stats = _normalize_messages_for_ledger(
        scoped_messages,
        start_marker=start_marker,
        end_marker=end_marker,
        order_id_digits=order_id_digits,
        require_hash=require_hash,
    )
    review_items = list(normalize_unstructured)

    daily_mode = _to_bool(config.get("daily_settlement_mode", False), False)
    initialized_now = False
    if daily_mode:
        marked_messages = _mark_messages(
            normalized_messages,
            start_marker=start_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
        )
        messages_for_parse, initialized_now = apply_daily_settlement_mode(marked_messages, config, trace=trace)
    else:
        messages_for_parse = filter_messages_by_hash_time_window(normalized_messages, config, trace=trace)

    parsed_records, parse_failures = _parse_messages_with_failures(
        messages_for_parse,
        start_marker=start_marker,
        end_marker=end_marker,
    )
    known_store_lookup = build_known_store_lookup(
        config,
        enabled_key="known_store_lookup_enabled",
        path_key="known_store_lookup_path",
        default_enabled=DEFAULT_KNOWN_STORE_LOOKUP_ENABLED,
        default_path=DEFAULT_KNOWN_STORE_LOOKUP_PATH,
        trace=trace,
    )
    known_store_hits = annotate_records_with_known_stores(
        parsed_records,
        known_store_lookup,
        max_matches=_to_int(
            config.get("known_store_lookup_max_matches", DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES),
            DEFAULT_KNOWN_STORE_LOOKUP_MAX_MATCHES,
            minimum=1,
        ),
    )
    known_store_gate_enabled = _to_bool(config.get("known_store_gate_enabled", True), True)
    gated_records, store_gate_rejects, known_store_rejected = _enforce_known_store_gate(
        parsed_records,
        known_store_lookup,
        enabled=known_store_gate_enabled and bool(known_store_lookup.get("enabled", False)),
    )
    review_items.extend(parse_failures)
    review_items.extend(store_gate_rejects)

    deduplicate_within_run = _to_bool(config.get("deduplicate_within_run", True), True)
    if deduplicate_within_run:
        unique_records = deduplicate_records(gated_records)
    else:
        unique_records = gated_records

    written_csv_rows = write_records(data_path, unique_records)

    json_output_enabled = _to_bool(config.get("json_output_enabled", True), True)
    json_output_path = ""
    written_json_bills = 0
    if json_output_enabled:
        json_path = Path(str(config.get("json_output_path", DEFAULT_JSON_OUTPUT_PATH)))
        append_history = _to_bool(config.get("json_output_append_history", True), True)
        bills = build_bill_payloads(unique_records)
        written_json_bills = write_bill_json_output(json_path, bills, append_history=append_history)
        json_output_path = str(json_path)

    return {
        "initialized_now": initialized_now,
        "input_messages": len(messages),
        "scoped_messages": len(scoped_messages),
        "normalized_messages": len(normalized_messages),
        "salvage_normalized_messages": int(normalize_stats.get("salvage_count", 0)),
        "unstructured_messages": int(normalize_stats.get("unstructured_count", 0)),
        "parse_input_messages": len(messages_for_parse),
        "parsed_records": len(parsed_records),
        "accepted_records": len(gated_records),
        "written_csv_rows": written_csv_rows,
        "written_json_bills": written_json_bills,
        "csv_path": str(data_path),
        "json_path": json_output_path,
        "normalization_schema_version": PAYLOAD_SCHEMA_VERSION,
        "known_store_lookup_enabled": bool(known_store_lookup.get("enabled", False)),
        "known_store_gate_enabled": known_store_gate_enabled,
        "known_store_hits": known_store_hits,
        "known_store_rejected": known_store_rejected,
        "review_items": review_items,
    }


class DifyReceiverServer(ThreadingHTTPServer):
    runtime_config: Dict[str, Any]
    trace: TraceFn
    ingest_path: str
    health_path: str
    max_payload_bytes: int
    review_queue_path: Path
    review_queue_enabled: bool
    default_source_id: str
    api_token: str
    process_lock: threading.Lock


class DifyReceiverHandler(BaseHTTPRequestHandler):
    server_version = "ZongziDifyReceiver/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        message = format % args
        _trace(getattr(self.server, "trace", None), f"[DIFY] HTTP {message}")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        health_path = getattr(self.server, "health_path", DEFAULT_DIFY_HEALTH_PATH)
        if path != health_path:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": "dify_receiver",
                "time": _now_text(),
            },
        )

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        ingest_path = getattr(self.server, "ingest_path", DEFAULT_DIFY_INGEST_PATH)
        if path != ingest_path:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

        if not request_is_authorized(
            self.headers,
            self.client_address,
            getattr(self.server, "api_token", DEFAULT_DIFY_API_TOKEN),
        ):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return

        max_bytes = min(
            DEFAULT_DIFY_MAX_PAYLOAD_BYTES,
            max(1024, int(getattr(self.server, "max_payload_bytes", DEFAULT_DIFY_MAX_PAYLOAD_BYTES))),
        )
        content_length = self.headers.get("Content-Length", "").strip()
        try:
            body_len = int(content_length) if content_length else 0
        except Exception:
            body_len = 0
        if body_len <= 0:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "empty_body"})
            return
        if body_len > max_bytes:
            self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"ok": False, "error": "payload_too_large", "max_payload_bytes": max_bytes},
            )
            return

        try:
            self.connection.settimeout(10)
        except OSError:
            pass
        raw_body = self.rfile.read(body_len)
        if len(raw_body) != body_len:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "incomplete_body"})
            return
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            queue_items = [
                _build_review_item(
                    "ingest",
                    "invalid_json",
                    extra={"body_excerpt": _compact_text(raw_body.decode("utf-8", errors="ignore"))},
                )
            ]
            if getattr(self.server, "review_queue_enabled", True):
                append_review_queue(getattr(self.server, "review_queue_path"), queue_items, trace=self.server.trace)
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid_json"})
            return

        accepted, normalize_failures = normalize_dify_payload(
            payload,
            default_source_id=getattr(self.server, "default_source_id", DEFAULT_DIFY_SOURCE_ID),
        )

        with getattr(self.server, "process_lock"):
            process_result: Dict[str, Any]
            try:
                process_result = process_dify_messages(accepted, self.server.runtime_config, trace=self.server.trace)
            except Exception as exc:
                failure_items = normalize_failures + [
                    _build_review_item(
                        "process",
                        "unexpected_error",
                        extra={"error": str(exc), "traceback": traceback.format_exc(limit=6)},
                    )
                ]
                if getattr(self.server, "review_queue_enabled", True):
                    append_review_queue(
                        getattr(self.server, "review_queue_path"),
                        failure_items,
                        trace=self.server.trace,
                    )
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "process_failed"})
                return

            review_items = list(normalize_failures)
            review_items.extend(process_result.pop("review_items", []))
            review_queued = 0
            if getattr(self.server, "review_queue_enabled", True):
                review_queued = append_review_queue(
                    getattr(self.server, "review_queue_path"),
                    review_items,
                    trace=self.server.trace,
                )

        response = {
            "ok": True,
            "result": process_result,
            "normalize_failures": len(normalize_failures),
            "review_queued": review_queued,
            "review_queue_path": str(getattr(self.server, "review_queue_path")),
            "normalization_schema_version": PAYLOAD_SCHEMA_VERSION,
        }
        self._send_json(HTTPStatus.OK, response)


def main() -> None:
    print("[启动] Dify 本地接收服务启动中。")
    # 启动接收服务时也补齐可选依赖，避免后续导出阶段才报缺包。
    if not check_environment(include_optional=True):
        return

    try:
        config = load_config(DEFAULT_CONFIG_PATH)
    except Exception as exc:
        print(f"[错误] 读取配置失败：{exc}")
        return

    dotenv_data = load_dotenv(DEFAULT_DOTENV_PATH)
    config, dotenv_applied = apply_dotenv_overrides(config, dotenv_data)

    logger, log_path = init_run_logger(config)
    trace = _make_trace(logger)
    trace(f"[DIFY] 启动时间={_now_text()}")
    trace(f"[DIFY] 管理员权限={'是' if _is_admin() else '否'}")
    if dotenv_applied > 0:
        trace(f"[DIFY] 已加载 .env 覆盖项数量={dotenv_applied}")

    ensure_csv_file(Path(str(config.get("data_path", DEFAULT_DATA_PATH))))

    try:
        listen_host = normalize_loopback_bind_host(
            _sanitize_csv_text(config.get("dify_listen_host", DEFAULT_DIFY_LISTEN_HOST))
            or DEFAULT_DIFY_LISTEN_HOST,
            default=DEFAULT_DIFY_LISTEN_HOST,
        )
    except ValueError as exc:
        print(f"[错误] 拒绝非回环监听地址：{type(exc).__name__}")
        return
    listen_port = _to_int(config.get("dify_listen_port", DEFAULT_DIFY_LISTEN_PORT), DEFAULT_DIFY_LISTEN_PORT, minimum=1)
    ingest_path = _sanitize_csv_text(config.get("dify_ingest_path", DEFAULT_DIFY_INGEST_PATH)) or DEFAULT_DIFY_INGEST_PATH
    health_path = _sanitize_csv_text(config.get("dify_health_path", DEFAULT_DIFY_HEALTH_PATH)) or DEFAULT_DIFY_HEALTH_PATH
    max_payload_bytes = _to_int(
        config.get("dify_max_payload_bytes", DEFAULT_DIFY_MAX_PAYLOAD_BYTES),
        DEFAULT_DIFY_MAX_PAYLOAD_BYTES,
        minimum=1024,
    )
    review_queue_path = Path(str(config.get("review_queue_path", DEFAULT_REVIEW_QUEUE_PATH)))
    review_queue_enabled = _to_bool(config.get("review_queue_enabled", True), True)
    default_source_id = _sanitize_csv_text(config.get("dify_source_id", DEFAULT_DIFY_SOURCE_ID)) or DEFAULT_DIFY_SOURCE_ID

    server = DifyReceiverServer((listen_host, listen_port), DifyReceiverHandler)
    server.runtime_config = config
    server.trace = trace
    server.ingest_path = ingest_path if ingest_path.startswith("/") else f"/{ingest_path}"
    server.health_path = health_path if health_path.startswith("/") else f"/{health_path}"
    server.max_payload_bytes = max_payload_bytes
    server.review_queue_path = review_queue_path
    server.review_queue_enabled = review_queue_enabled
    server.default_source_id = default_source_id
    server.api_token = _sanitize_csv_text(config.get("dify_api_token", DEFAULT_DIFY_API_TOKEN))
    server.process_lock = threading.Lock()

    print(f"[日志] 抓取日志文件：{log_path}")
    print(f"[监听] http://{listen_host}:{listen_port}{server.ingest_path}")
    print(f"[健康] http://{listen_host}:{listen_port}{server.health_path}")
    print("[提示] 按 Ctrl+C 停止服务。")

    trace(
        f"[DIFY] 服务监听: host={listen_host}, port={listen_port}, ingest_path={server.ingest_path}, review_queue_enabled={review_queue_enabled}, review_queue_path={review_queue_path}"
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[停止] 收到中断，服务退出。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
