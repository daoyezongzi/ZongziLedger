from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from core.constants import (
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
from core.parser import parse_ledger_message_multi
from core.store_lookup import annotate_records_with_known_stores, build_known_store_lookup
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

MESSAGE_TEXT_KEYS: Tuple[str, ...] = ("message", "raw_message", "content", "text", "body")
TIMESTAMP_KEYS: Tuple[str, ...] = ("timestamp", "message_time", "created_at", "event_time", "time")
SOURCE_KEYS: Tuple[str, ...] = ("source_id", "chat_name", "chat_id", "conversation_id", "source")


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

    for key in ("messages", "ledger_messages", "records", "items"):
        value = payload.get(key)
        if isinstance(value, list):
            return value

    for key in ("outputs", "data", "result"):
        nested = payload.get(key)
        if isinstance(nested, list):
            return nested
        if isinstance(nested, dict):
            for nested_key in ("messages", "ledger_messages", "records", "items"):
                nested_value = nested.get(nested_key)
                if isinstance(nested_value, list):
                    return nested_value
            if _pick_text_from_map(nested, MESSAGE_TEXT_KEYS):
                return [nested]

    if _pick_text_from_map(payload, MESSAGE_TEXT_KEYS):
        return [payload]
    return []


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


def normalize_dify_payload(
    payload: Any,
    *,
    default_source_id: str = DEFAULT_DIFY_SOURCE_ID,
) -> Tuple[List[Dict[str, str]], List[Dict[str, Any]]]:
    entries = _extract_candidate_entries(payload)
    now_text = _now_text()

    source_from_payload = ""
    if isinstance(payload, dict):
        source_from_payload = _pick_text_from_map(payload, SOURCE_KEYS)
        if not source_from_payload and isinstance(payload.get("meta"), dict):
            source_from_payload = _pick_text_from_map(payload.get("meta", {}), SOURCE_KEYS)

    accepted: List[Dict[str, str]] = []
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

        accepted.append(
            {
                "timestamp": timestamp,
                "message": raw_message,
                "source_id": source_id,
                "message_hash": message_hash or _fingerprint_text(raw_message),
                "message_captured_at": message_captured_at or now_text,
            }
        )

    return accepted, rejected


def _parse_messages_with_failures(
    messages: List[Dict[str, str]],
    start_marker: str,
    end_marker: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    records: List[Dict[str, Any]] = []
    failures: List[Dict[str, Any]] = []
    for message in messages:
        raw_message = _sanitize_csv_text(message.get("message", ""))
        parsed = parse_ledger_message_multi(raw_message, start_marker, end_marker=end_marker)
        if not parsed:
            failures.append(
                _build_review_item(
                    "parse",
                    "parse_failed",
                    raw_message,
                    source_id=_sanitize_csv_text(message.get("source_id", "")),
                    timestamp=_sanitize_csv_text(message.get("timestamp", "")),
                    message_hash=_sanitize_csv_text(message.get("message_hash", "")),
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


def _collect_invalid_order_messages(
    messages: List[Dict[str, str]],
    start_marker: str,
    order_id_digits: int,
    require_hash: bool,
) -> List[Dict[str, Any]]:
    failures: List[Dict[str, Any]] = []
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
        if order_id:
            continue
        failures.append(
            _build_review_item(
                "validate",
                "invalid_order_header",
                raw_message,
                source_id=_sanitize_csv_text(message.get("source_id", "")),
                timestamp=_sanitize_csv_text(message.get("timestamp", "")),
                message_hash=_sanitize_csv_text(message.get("message_hash", "")),
            )
        )
    return failures


def process_dify_messages(
    messages: List[Dict[str, str]],
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
    review_items = _collect_invalid_order_messages(
        scoped_messages,
        start_marker=start_marker,
        order_id_digits=order_id_digits,
        require_hash=require_hash,
    )

    daily_mode = _to_bool(config.get("daily_settlement_mode", False), False)
    initialized_now = False
    if daily_mode:
        marked_messages = _mark_messages(
            scoped_messages,
            start_marker=start_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
        )
        messages_for_parse, initialized_now = apply_daily_settlement_mode(marked_messages, config, trace=trace)
    else:
        messages_for_parse = filter_messages_by_hash_time_window(scoped_messages, config, trace=trace)

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
    review_items.extend(parse_failures)

    deduplicate_within_run = _to_bool(config.get("deduplicate_within_run", True), True)
    if deduplicate_within_run:
        unique_records = deduplicate_records(parsed_records)
    else:
        unique_records = parsed_records

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
        "parse_input_messages": len(messages_for_parse),
        "parsed_records": len(parsed_records),
        "written_csv_rows": written_csv_rows,
        "written_json_bills": written_json_bills,
        "csv_path": str(data_path),
        "json_path": json_output_path,
        "known_store_lookup_enabled": bool(known_store_lookup.get("enabled", False)),
        "known_store_hits": known_store_hits,
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

        max_bytes = int(getattr(self.server, "max_payload_bytes", DEFAULT_DIFY_MAX_PAYLOAD_BYTES))
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

        raw_body = self.rfile.read(body_len)
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
        }
        self._send_json(HTTPStatus.OK, response)


def main() -> None:
    print("[启动] Dify 本地接收服务启动中。")
    if not check_environment():
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

    listen_host = _sanitize_csv_text(config.get("dify_listen_host", DEFAULT_DIFY_LISTEN_HOST)) or DEFAULT_DIFY_LISTEN_HOST
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
