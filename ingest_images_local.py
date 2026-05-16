from __future__ import annotations

import json
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from core.constants import DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH, DEFAULT_VISION_REVIEW_QUEUE_PATH
from main import _make_trace, init_run_logger, load_runtime_config, process_ledger_messages
from setup import check_environment

def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _as_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}


def _normalize_collect_result(result: Any) -> Tuple[List[Dict[str, Any]], List[Any], Dict[str, Any]]:
    if isinstance(result, dict):
        messages = _as_list(result.get("messages"))
        failures = _as_list(result.get("failures"))
        stats = _as_dict(result.get("stats"))
        return messages, failures, stats

    if isinstance(result, tuple) and len(result) >= 3:
        messages = _as_list(result[0])
        failures = _as_list(result[1])
        stats = _as_dict(result[2])
        return messages, failures, stats

    raise RuntimeError("collect_messages_from_images must return dict or (messages, failures, stats)")


def _normalize_messages(messages: Sequence[Any]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("message", "")).strip()
        if not text:
            continue
        normalized.append(dict(item))
    return normalized


def append_vision_review_queue(queue_path: Path, failures: Sequence[Any]) -> int:
    if not failures:
        return 0

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    wrote = 0
    with queue_path.open("a", encoding="utf-8") as f:
        for item in failures:
            f.write(json.dumps(_to_json_safe(item), ensure_ascii=False) + "\n")
            wrote += 1
    return wrote


def _print_image_scan_stats(stats: Dict[str, Any], messages_count: int) -> None:
    payload = dict(stats)
    payload.setdefault("messages_count", messages_count)
    print("[图片扫描统计]")
    print(json.dumps(payload, ensure_ascii=False))


def _print_ledger_stats(result: Dict[str, Any]) -> None:
    written_records_count = int(result.get("written_records_count", 0) or 0)
    json_written_bills = int(result.get("json_written_bills", 0) or 0)
    print("[入账统计]")
    print(f"written_records_count={written_records_count}")
    print(f"json_written_bills={json_written_bills}")


def run() -> int:
    print("[启动] 本地图片入账流程开始。")
    if not check_environment(include_optional=True):
        return 1

    try:
        config, dotenv_applied = load_runtime_config(DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH)
    except Exception as exc:
        print(f"[错误] 读取配置失败：{exc}")
        return 1

    logger, log_path = init_run_logger(config)
    trace = _make_trace(logger)
    trace(f"[IMAGE-INGEST] 启动时间={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if dotenv_applied > 0:
        trace(f"[IMAGE-INGEST] 已加载 .env 覆盖项数量={dotenv_applied}")

    try:
        from core.vision_ingest import collect_messages_from_images
    except Exception as exc:
        trace(f"[IMAGE-INGEST] 无法导入 core.vision_ingest.collect_messages_from_images: {exc}")
        trace(traceback.format_exc())
        print("[错误] 缺少 core.vision_ingest.collect_messages_from_images。")
        print(f"[提示] 详细日志：{log_path}")
        return 1

    try:
        collected = collect_messages_from_images(config, trace=trace)
        messages, failures, stats = _normalize_collect_result(collected)
        messages = _normalize_messages(messages)
    except Exception as exc:
        trace(f"[IMAGE-INGEST] 图片扫描失败: {exc}")
        trace(traceback.format_exc())
        print(f"[错误] 图片扫描失败：{exc}")
        print(f"[提示] 详细日志：{log_path}")
        return 1

    ledger_result: Dict[str, Any] = {"written_records_count": 0, "json_written_bills": 0}
    if messages:
        try:
            ledger_result = process_ledger_messages(messages, config, trace=trace, workflow="capture")
        except Exception as exc:
            trace(f"[IMAGE-INGEST] 入账处理失败: {exc}")
            trace(traceback.format_exc())
            print(f"[错误] 入账处理失败：{exc}")
            print(f"[提示] 详细日志：{log_path}")
            return 1

    queue_path = Path(str(config.get("vision_review_queue_path", DEFAULT_VISION_REVIEW_QUEUE_PATH)))
    queued_count = 0
    if failures:
        try:
            queued_count = append_vision_review_queue(queue_path, failures)
            trace(f"[IMAGE-INGEST] failures 写入复核队列: path={queue_path}, count={queued_count}")
        except Exception as exc:
            trace(f"[IMAGE-INGEST] failures 写入复核队列失败: {exc}")
            trace(traceback.format_exc())
            print(f"[错误] 写入复核队列失败：{exc}")
            print(f"[提示] 详细日志：{log_path}")
            return 1

    _print_image_scan_stats(stats, len(messages))
    _print_ledger_stats(ledger_result)
    print(f"[失败条数] {len(failures)}")
    if failures:
        print(f"[复核队列] path={queue_path} appended={queued_count}")
    print(f"[日志] {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
