from __future__ import annotations

import json
import re
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.constants import (
    DEFAULT_END_MARKER,
    DEFAULT_DIFY_API_HOST,
    DEFAULT_DIFY_API_PORT,
    DEFAULT_DIFY_AUDIT_ENABLED,
    DEFAULT_DIFY_AUDIT_PATH,
    DEFAULT_DIFY_INGEST_PATH,
    DEFAULT_DIFY_REMOTE_API_KEY,
    DEFAULT_DIFY_REMOTE_API_URL,
    DEFAULT_DIFY_REMOTE_ENABLED,
    DEFAULT_DIFY_REMOTE_FALLBACK_LOCAL,
    DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY,
    DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY,
    DEFAULT_DIFY_REMOTE_INPUTS_AS_JSON_TEXT,
    DEFAULT_DIFY_REMOTE_PRIORITY,
    DEFAULT_DIFY_REMOTE_RESPONSE_MODE,
    DEFAULT_DIFY_REMOTE_TIMEOUT_SECONDS,
    DEFAULT_DIFY_REMOTE_USER_AGENT,
    DEFAULT_DIFY_REMOTE_USER,
    DEFAULT_DIFY_RESPONSE_PREVIEW_LIMIT,
    resolve_end_marker,
    resolve_start_marker,
)
from core.parser import parse_ledger_message_multi
from main import (
    init_run_logger,
    load_runtime_config,
    process_ledger_messages,
)


SERVICE_NAME = "zongziledger-dify-bridge"
MESSAGE_LIST_KEYS = (
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
REMOTE_MESSAGE_LIST_KEYS = ("normalized_messages",)
MESSAGE_TEXT_KEYS = (
    "message",
    "normalized_block",
    "cleaned_message",
    "raw_text",
    "raw_message",
    "text",
    "content",
    "answer",
)
MESSAGE_NAME_KEYS = ("name", "customer_name", "buyer_name", "contact_name")
CANDIDATE_FLAG_KEYS = ("is_candidate", "candidate", "should_clean", "need_cleaning")
PAYLOAD_SCHEMA_VERSION = "zongziledger-dify-clean-v1"
ORDER_HEADER_RE = re.compile(r"^\s*[#\uFF03]?\s*.+?\d{8}\s*$")
ITEM_LINE_RE = re.compile(r".*?\d+(?:\.\d+)?\s*$")


def _normalize_path(path: str, fallback: str = "/") -> str:
    value = _safe_text(path) or fallback
    if not value.startswith("/"):
        value = f"/{value}"
    if len(value) > 1:
        value = value.rstrip("/")
    return value or fallback


def _safe_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _to_int(value: Any, default: int, minimum: Optional[int] = None) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if minimum is not None and parsed < minimum:
        return minimum
    return parsed


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "\u662f"}:
        return True
    if text in {"0", "false", "no", "n", "off", "\u5426"}:
        return False
    return default


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _looks_like_ledger_message(text: str) -> bool:
    normalized = _safe_text(text)
    if not normalized:
        return False
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if len(lines) < 2:
        return False
    if ORDER_HEADER_RE.fullmatch(lines[0]):
        item_hits = sum(1 for line in lines[1:] if ITEM_LINE_RE.fullmatch(line))
        return item_hits > 0
    item_hits_all = sum(1 for line in lines if ITEM_LINE_RE.fullmatch(line))
    if item_hits_all > 0:
        return True
    item_hits_payload = sum(1 for line in lines[1:] if ITEM_LINE_RE.fullmatch(line))
    return item_hits_payload > 0


def _classify_normalize_hint(message_text: str, start_marker: str, end_marker: str) -> str:
    normalized = _safe_text(message_text)
    if not normalized:
        return "fallback_sample"
    lines = [line.strip() for line in normalized.split("\n") if line.strip()]
    if not lines:
        return "fallback_sample"

    has_start = any(start_marker in line for line in lines)
    has_end = any(line == end_marker for line in lines)
    if has_start and has_end and _looks_like_ledger_message(normalized):
        return "structured_candidate"
    if has_start or _looks_like_ledger_message(normalized):
        return "salvage_candidate"
    return "fallback_sample"


def _extract_message_text(item: Any) -> str:
    if isinstance(item, str):
        return _safe_text(item)
    if not isinstance(item, dict):
        return ""
    for key in MESSAGE_TEXT_KEYS:
        text = _safe_text(item.get(key, ""))
        if text:
            return text
    return ""


def _is_candidate_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    for key in CANDIDATE_FLAG_KEYS:
        if key not in item:
            continue
        return _to_bool(item.get(key), False)
    return True


def _build_message_item(item: Any, default_source_id: str, default_timestamp: str) -> Optional[Dict[str, Any]]:
    if not _is_candidate_item(item):
        return None
    message_text = _extract_message_text(item)
    if not message_text:
        return None

    source_id = default_source_id
    timestamp = default_timestamp
    message_hash = ""
    message_captured_at = ""
    name = ""
    store_name = ""

    if isinstance(item, dict):
        source_id = _safe_text(item.get("source_id", default_source_id)) or default_source_id
        timestamp = _safe_text(item.get("timestamp", default_timestamp)) or default_timestamp
        message_hash = _safe_text(item.get("message_hash", ""))
        message_captured_at = _safe_text(item.get("message_captured_at", ""))
        for key in MESSAGE_NAME_KEYS:
            name = _safe_text(item.get(key, ""))
            if name:
                break
        store_name = _safe_text(item.get("store_name", ""))

    output: Dict[str, Any] = {
        "message": message_text,
        "source_id": source_id,
        "timestamp": timestamp,
    }
    if isinstance(item, dict):
        raw_message = _safe_text(item.get("raw_message", "")) or message_text
        output["raw_message"] = raw_message
        normalize_hint = _safe_text(item.get("normalize_hint", ""))
        if normalize_hint:
            output["normalize_hint"] = normalize_hint
        raw_context = item.get("raw_context")
        if isinstance(raw_context, dict):
            output["raw_context"] = _to_json_safe(raw_context)
    if message_hash:
        output["message_hash"] = message_hash
    if message_captured_at:
        output["message_captured_at"] = message_captured_at
    if name:
        output["name"] = name
    if store_name:
        output["store_name"] = store_name
    return output


def _parse_json_text(value: Any) -> Any:
    if not isinstance(value, str):
        return None
    text = _safe_text(value)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _message_list_from_value(value: Any) -> Optional[List[Any]]:
    if isinstance(value, list):
        return value
    parsed = _parse_json_text(value)
    if isinstance(parsed, list):
        return parsed
    return None


def _append_message_items(
    collector: List[Dict[str, Any]],
    payload_like: Any,
    *,
    default_source_id: str,
    default_timestamp: str,
) -> int:
    added_before = len(collector)

    if isinstance(payload_like, list):
        for item in payload_like:
            message_item = _build_message_item(
                item,
                default_source_id=default_source_id,
                default_timestamp=default_timestamp,
            )
            if message_item is not None:
                collector.append(message_item)
        return len(collector) - added_before

    if isinstance(payload_like, dict):
        for key in MESSAGE_LIST_KEYS:
            value = payload_like.get(key)
            if isinstance(value, list):
                _append_message_items(
                    collector,
                    value,
                    default_source_id=default_source_id,
                    default_timestamp=default_timestamp,
                )
        if len(collector) > added_before:
            return len(collector) - added_before

        message_item = _build_message_item(
            payload_like,
            default_source_id=default_source_id,
            default_timestamp=default_timestamp,
        )
        if message_item is not None:
            collector.append(message_item)
        return len(collector) - added_before

    return 0


def build_messages_from_payload(
    payload: Dict[str, Any],
    *,
    messages_key: str = DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY,
    payload_key: str = DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY,
) -> List[Dict[str, Any]]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    default_source_id = _safe_text(payload.get("source_id", "dify")) or "dify"
    default_timestamp = _safe_text(payload.get("timestamp", now_text)) or now_text

    messages: List[Dict[str, Any]] = []
    key_candidates = []
    for key in (
        "messages",
        messages_key,
        payload_key,
        "messages_json",
        "payload_json",
        "inputs",
        "cleaning_input",
    ):
        normalized_key = _safe_text(key)
        if normalized_key and normalized_key not in key_candidates:
            key_candidates.append(normalized_key)

    for key in key_candidates:
        if key not in payload:
            continue
        value = payload.get(key)
        added = _append_message_items(
            messages,
            value,
            default_source_id=default_source_id,
            default_timestamp=default_timestamp,
        )
        if added > 0:
            continue
        parsed_value = _parse_json_text(value)
        if parsed_value is None:
            continue
        _append_message_items(
            messages,
            parsed_value,
            default_source_id=default_source_id,
            default_timestamp=default_timestamp,
        )

    if messages:
        return messages

    single_item = _build_message_item(payload, default_source_id=default_source_id, default_timestamp=default_timestamp)
    if single_item is not None:
        messages.append(single_item)
    return messages


def _append_audit_record(audit_path: Path, payload: Dict[str, Any]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    existing_records: List[Dict[str, Any]] = []
    if audit_path.exists():
        try:
            with audit_path.open("r", encoding="utf-8") as f:
                previous_payload = json.load(f)
            if isinstance(previous_payload, dict) and isinstance(previous_payload.get("records"), list):
                existing_records = [x for x in previous_payload.get("records", []) if isinstance(x, dict)]
            elif isinstance(previous_payload, list):
                existing_records = [x for x in previous_payload if isinstance(x, dict)]
        except Exception:
            # 向后兼容旧版 jsonl：逐行读取可解析对象
            try:
                with audit_path.open("r", encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        obj = json.loads(line)
                        if isinstance(obj, dict):
                            existing_records.append(obj)
            except Exception:
                existing_records = []

    existing_records.append(payload)
    output = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "records": existing_records,
    }
    with audit_path.open("w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)


def _http_post_json(url: str, payload: Dict[str, Any], headers: Dict[str, str], timeout_seconds: int) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=_json_compact(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout_seconds))) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URL error: {exc}") from exc

    try:
        parsed = json.loads(body_text)
    except Exception as exc:
        raise RuntimeError(f"remote response is not valid JSON: {body_text[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("remote response root must be JSON object")
    return parsed


def _extract_remote_payload(candidate: Any) -> Dict[str, Any]:
    if isinstance(candidate, str):
        text = _safe_text(candidate)
        if not text:
            return {}
        parsed = _parse_json_text(text)
        if parsed is not None:
            return _extract_remote_payload(parsed)
        return {}

    if isinstance(candidate, list):
        return {}

    if not isinstance(candidate, dict):
        return {}

    for key in REMOTE_MESSAGE_LIST_KEYS:
        messages = _message_list_from_value(candidate.get(key))
        if messages is not None:
            payload = dict(candidate)
            payload["messages"] = messages
            return payload

    for nested_key in ("outputs", "result", "output", "payload", "data"):
        nested = candidate.get(nested_key)
        nested_payload = _extract_remote_payload(nested)
        if nested_payload:
            return nested_payload

    return {}


def _extract_remote_workflow_run_id(remote_response: Dict[str, Any]) -> str:
    candidates: List[Any] = [
        remote_response.get("workflow_run_id"),
        remote_response.get("workflow_run_id_str"),
        remote_response.get("task_id"),
    ]
    data = remote_response.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("workflow_run_id"),
                data.get("workflow_run_id_str"),
                data.get("id"),
                data.get("task_id"),
            ]
        )

    for value in candidates:
        text = _safe_text(value)
        if text:
            return text
    return ""


def _extract_remote_unstructured_samples(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    output: List[Dict[str, Any]] = []
    for key in ("unstructured_samples", "review_queue_samples", "failed_samples"):
        value = payload.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict):
                output.append(_to_json_safe(item))
            elif isinstance(item, str):
                output.append({"raw_message": _safe_text(item)})
    return output


def _resolve_remote_messages(remote_response: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    workflow_run_id = _extract_remote_workflow_run_id(remote_response)
    payload = _extract_remote_payload(remote_response)
    if not payload:
        data = remote_response.get("data")
        if isinstance(data, dict):
            payload = _extract_remote_payload(data)
    if not payload:
        return [], {
            "unstructured_samples_count": 0,
            "schema_version": "",
            "workflow_run_id": workflow_run_id,
            "normalized_count": 0,
        }

    messages = build_messages_from_payload(payload)
    unstructured_samples = _extract_remote_unstructured_samples(payload)

    normalization_summary: Dict[str, Any] = {
        "schema_version": _safe_text(payload.get("schema_version", "")),
        "unstructured_samples_count": len(unstructured_samples),
        "workflow_run_id": workflow_run_id,
        "normalized_count": len(messages),
    }
    remote_summary = payload.get("normalization_summary")
    if isinstance(remote_summary, dict):
        for key in (
            "input_messages_count",
            "structured_count",
            "salvage_candidate_count",
            "fallback_sample_count",
            "normalized_count",
            "review_queue_count",
        ):
            if key in remote_summary:
                normalization_summary[key] = _to_json_safe(remote_summary.get(key))
    if unstructured_samples:
        normalization_summary["unstructured_samples_preview"] = unstructured_samples[:3]

    return messages, normalization_summary


def _build_cleaning_context(messages: List[Dict[str, Any]], config: Dict[str, Any]) -> Dict[str, Any]:
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config, fallback=DEFAULT_END_MARKER)

    structured_candidates: List[Dict[str, Any]] = []
    salvage_candidates: List[Dict[str, Any]] = []
    fallback_samples: List[Dict[str, Any]] = []
    for idx, message in enumerate(messages):
        raw = _safe_text(message.get("raw_message", "")) or _safe_text(message.get("message", ""))
        hint = _safe_text(message.get("normalize_hint", ""))
        if not hint:
            hint = _classify_normalize_hint(raw, start_marker=start_marker, end_marker=end_marker)
        sample = {
            "index": idx,
            "message": _safe_text(message.get("message", "")),
            "raw_message": raw,
            "timestamp": _safe_text(message.get("timestamp", "")),
            "source_id": _safe_text(message.get("source_id", "")),
            "message_hash": _safe_text(message.get("message_hash", "")),
        }
        raw_context = message.get("raw_context")
        if isinstance(raw_context, dict):
            sample["raw_context"] = _to_json_safe(raw_context)
        if hint == "structured_candidate":
            structured_candidates.append(sample)
        elif hint == "salvage_candidate":
            salvage_candidates.append(sample)
        else:
            fallback_samples.append(sample)

    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "strategy": "remote_first_cleaning",
        "start_marker": start_marker,
        "end_marker": end_marker,
        "input_messages_count": len(messages),
        "structured_count": len(structured_candidates),
        "salvage_candidate_count": len(salvage_candidates),
        "fallback_sample_count": len(fallback_samples),
        "structured_candidates": structured_candidates,
        "salvage_candidates": salvage_candidates,
        "fallback_samples": fallback_samples,
    }


def _is_valid_ledger_block_message(
    message_text: str,
    start_marker: str,
    end_marker: str,
) -> bool:
    text = _safe_text(message_text)
    if not text:
        return False
    parsed = parse_ledger_message_multi(text, start_marker=start_marker, end_marker=end_marker)
    return bool(parsed)


def _filter_remote_messages_for_ledger(
    messages: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], int]:
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config, fallback=DEFAULT_END_MARKER)

    valid_messages: List[Dict[str, Any]] = []
    invalid_count = 0
    for message in messages:
        raw = _safe_text(message.get("message", "")) if isinstance(message, dict) else ""
        if _is_valid_ledger_block_message(
            raw,
            start_marker=start_marker,
            end_marker=end_marker,
        ):
            valid_messages.append(message)
        else:
            invalid_count += 1
    return valid_messages, invalid_count


def _build_remote_inputs(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    payload_key = _safe_text(
        config.get("dify_remote_input_payload_key", DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY)
    ) or DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY
    messages_key = _safe_text(
        config.get("dify_remote_input_messages_key", DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY)
    ) or DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY
    as_json_text = _to_bool(
        config.get("dify_remote_inputs_as_json_text", DEFAULT_DIFY_REMOTE_INPUTS_AS_JSON_TEXT),
        DEFAULT_DIFY_REMOTE_INPUTS_AS_JSON_TEXT,
    )

    normalized_messages = build_messages_from_payload(
        payload,
        messages_key=messages_key,
        payload_key=payload_key,
    )
    cleaning_context = _build_cleaning_context(normalized_messages, config)
    first_message_text = normalized_messages[0]["message"] if normalized_messages else ""

    inputs: Dict[str, Any] = {}
    if as_json_text:
        inputs[payload_key] = _json_compact(payload)
        inputs[messages_key] = _json_compact(normalized_messages)
        inputs["normalization_context_json"] = _json_compact(cleaning_context)
    else:
        inputs[payload_key] = payload
        inputs[messages_key] = normalized_messages
        inputs["normalization_context"] = cleaning_context

    # Keep a stable key for new cleaning workflows while preserving existing payload keys.
    inputs["cleaning_input"] = cleaning_context

    if first_message_text:
        inputs.setdefault("query", first_message_text)

    return inputs


def _call_remote_dify(payload: Dict[str, Any], config: Dict[str, Any]) -> Dict[str, Any]:
    remote_api_url = _safe_text(config.get("dify_remote_api_url", DEFAULT_DIFY_REMOTE_API_URL))
    remote_api_key = _safe_text(config.get("dify_remote_api_key", DEFAULT_DIFY_REMOTE_API_KEY))
    if not remote_api_url:
        raise RuntimeError("dify_remote_api_url is empty")
    if not remote_api_key:
        raise RuntimeError("dify_remote_api_key is empty")

    timeout_seconds = _to_int(
        config.get("dify_remote_timeout_seconds", DEFAULT_DIFY_REMOTE_TIMEOUT_SECONDS),
        DEFAULT_DIFY_REMOTE_TIMEOUT_SECONDS,
        minimum=1,
    )
    response_mode = _safe_text(
        config.get("dify_remote_response_mode", DEFAULT_DIFY_REMOTE_RESPONSE_MODE)
    ) or DEFAULT_DIFY_REMOTE_RESPONSE_MODE
    remote_user = _safe_text(config.get("dify_remote_user", DEFAULT_DIFY_REMOTE_USER)) or DEFAULT_DIFY_REMOTE_USER
    remote_user_agent = _safe_text(
        config.get("dify_remote_user_agent", DEFAULT_DIFY_REMOTE_USER_AGENT)
    ) or DEFAULT_DIFY_REMOTE_USER_AGENT

    request_payload = {
        "inputs": _build_remote_inputs(payload, config),
        "response_mode": response_mode,
        "user": remote_user,
    }
    headers = {
        "Authorization": f"Bearer {remote_api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": remote_user_agent,
    }
    return _http_post_json(remote_api_url, request_payload, headers=headers, timeout_seconds=timeout_seconds)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class DifyBridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_payload(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return None, "missing Content-Length"
        try:
            size = int(content_length)
        except ValueError:
            return None, "invalid Content-Length"
        if size <= 0:
            return None, "empty request body"

        raw = self.rfile.read(size)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "request body is not valid JSON"
        if not isinstance(parsed, dict):
            return None, "request body must be JSON object"
        return parsed, None

    def do_GET(self) -> None:  # noqa: N802
        request_path = _normalize_path(self.path.split("?", 1)[0], "/")
        health_paths = getattr(self.server, "health_paths", {"/", "/health", "/api/health"})
        if request_path not in health_paths:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": f"unknown path: {self.path}", "service": SERVICE_NAME},
            )
            return
        self._write_json(
            HTTPStatus.OK,
            {
                "ok": True,
                "service": SERVICE_NAME,
                "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        request_path = _normalize_path(self.path.split("?", 1)[0], "/")
        ingest_path = _normalize_path(getattr(self.server, "ingest_path", DEFAULT_DIFY_INGEST_PATH))
        if request_path != ingest_path:
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"ok": False, "error": f"unknown path: {self.path}", "service": SERVICE_NAME},
            )
            return

        payload, err = self._read_payload()
        if err is not None or payload is None:
            self._write_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": err or "bad request"})
            return

        try:
            config, dotenv_applied = load_runtime_config()
        except Exception as exc:
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": f"failed to load config: {exc}"},
            )
            return

        logger, log_path = init_run_logger(config)
        trace = logger.info

        try:
            remote_enabled = _to_bool(
                config.get("dify_remote_enabled", DEFAULT_DIFY_REMOTE_ENABLED),
                DEFAULT_DIFY_REMOTE_ENABLED,
            )
            remote_priority = _to_bool(
                config.get("dify_remote_priority", DEFAULT_DIFY_REMOTE_PRIORITY),
                DEFAULT_DIFY_REMOTE_PRIORITY,
            )
            remote_fallback_local = _to_bool(
                config.get("dify_remote_fallback_local", DEFAULT_DIFY_REMOTE_FALLBACK_LOCAL),
                DEFAULT_DIFY_REMOTE_FALLBACK_LOCAL,
            )

            selected_messages: List[Dict[str, Any]] = []
            remote_error = ""
            route = "local"
            remote_response: Dict[str, Any] = {}
            remote_normalization: Dict[str, Any] = {}
            workflow_run_id = ""
            normalized_count = 0

            if remote_enabled:
                try:
                    remote_response = _call_remote_dify(payload, config)
                    workflow_run_id = _extract_remote_workflow_run_id(remote_response)
                    selected_messages, remote_normalization = _resolve_remote_messages(remote_response)
                    workflow_run_id = _safe_text(
                        remote_normalization.get("workflow_run_id", workflow_run_id)
                    )
                    if selected_messages:
                        raw_remote_count = len(selected_messages)
                        filtered_remote_messages, invalid_remote_messages = _filter_remote_messages_for_ledger(
                            selected_messages,
                            config,
                        )
                        if filtered_remote_messages:
                            selected_messages = filtered_remote_messages
                            route = "remote"
                            normalized_count = len(filtered_remote_messages)
                            remote_normalization["normalized_count"] = normalized_count
                            if invalid_remote_messages > 0:
                                trace(
                                    f"[DIFY] remote messages filtered: kept={len(filtered_remote_messages)}, dropped={invalid_remote_messages}"
                                )
                        else:
                            selected_messages = []
                            normalized_count = 0
                            remote_normalization["normalized_count"] = 0
                            remote_error = (
                                f"remote response has no valid ledger block message (raw_messages={raw_remote_count})"
                            )
                            trace(
                                "[DIFY] remote response resolved messages but none passed ledger-block validation"
                            )
                    else:
                        normalized_count = 0
                        remote_normalization["normalized_count"] = 0
                        remote_error = "remote response has no parseable ledger message"
                        trace(f"[DIFY] remote returned empty messages")
                except Exception as exc:
                    remote_error = str(exc)
                    route = "remote-failed"
                    trace(f"[DIFY] remote call failed: {exc}")

                if route != "remote" and remote_priority:
                    if remote_fallback_local:
                        trace(
                            "[DIFY] remote-priority failed, falling back to local processing "
                            f"because remote_fallback_local=true: {remote_error or 'unknown remote error'}"
                        )
                    else:
                        route = "remote-failed"
                        route_info = {
                            "mode": route,
                            "remote_enabled": remote_enabled,
                            "remote_priority": remote_priority,
                            "remote_fallback_local": remote_fallback_local,
                            "remote_error": remote_error or "unknown remote error",
                            "workflow_run_id": workflow_run_id,
                            "normalized_count": normalized_count,
                        }
                        if remote_normalization:
                            route_info["normalization"] = _to_json_safe(remote_normalization)
                        trace(
                            "[DIFY] ingest "
                            f"route={route}, remote_error={route_info['remote_error']}, "
                            f"workflow_run_id={workflow_run_id}, normalized_count={normalized_count}"
                        )
                        self._write_json(
                            HTTPStatus.BAD_GATEWAY,
                            {
                                "ok": False,
                                "error": f"remote-priority failed: {remote_error or 'unknown remote error'}",
                                "service": SERVICE_NAME,
                                "route": route_info,
                                "workflow_run_id": workflow_run_id,
                                "normalized_count": normalized_count,
                                "log_path": str(log_path),
                            },
                        )
                        return

            if not selected_messages:
                local_messages_key = _safe_text(
                    config.get("dify_remote_input_messages_key", DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY)
                ) or DEFAULT_DIFY_REMOTE_INPUT_MESSAGES_KEY
                local_payload_key = _safe_text(
                    config.get("dify_remote_input_payload_key", DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY)
                ) or DEFAULT_DIFY_REMOTE_INPUT_PAYLOAD_KEY
                selected_messages = build_messages_from_payload(
                    payload,
                    messages_key=local_messages_key,
                    payload_key=local_payload_key,
                )
                route = "local-fallback" if remote_enabled else "local"

            if not selected_messages:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {
                        "ok": False,
                        "error": "no processable ledger messages found in payload",
                    },
                )
                return

            trace(
                "[DIFY] ingest "
                f"route={route}, messages={len(selected_messages)}, dotenv_applied={dotenv_applied}, "
                f"remote_error={remote_error}, workflow_run_id={workflow_run_id}, "
                f"normalized_count={normalized_count if route == 'remote' else len(selected_messages)}"
            )
            result = process_ledger_messages(selected_messages, config, trace=trace, workflow="dify")

            preview_limit = _to_int(
                config.get("dify_response_preview_limit", DEFAULT_DIFY_RESPONSE_PREVIEW_LIMIT),
                DEFAULT_DIFY_RESPONSE_PREVIEW_LIMIT,
                minimum=0,
            )
            preview_records = list(result.get("records", []))
            if preview_limit >= 0:
                preview_records = preview_records[:preview_limit]

            route_info = {
                "mode": route,
                "remote_enabled": remote_enabled,
                "remote_priority": remote_priority,
                "remote_fallback_local": remote_fallback_local,
            }
            if remote_error:
                route_info["remote_error"] = remote_error
            if workflow_run_id:
                route_info["workflow_run_id"] = workflow_run_id
            route_info["normalized_count"] = normalized_count if route == "remote" else len(selected_messages)
            if remote_normalization:
                route_info["normalization"] = _to_json_safe(remote_normalization)

            response_payload: Dict[str, Any] = {
                "ok": True,
                "service": SERVICE_NAME,
                "workflow": "dify",
                "normalization_schema_version": PAYLOAD_SCHEMA_VERSION,
                "route": route_info,
                "status": str(result.get("status", "ok")),
                "initialized_now": bool(result.get("initialized_now", False)),
                "start_marker": str(result.get("start_marker", "")),
                "end_marker": str(result.get("end_marker", "")),
                "input_messages_count": int(result.get("input_messages_count", 0)),
                "scoped_messages_count": int(result.get("scoped_messages_count", 0)),
                "messages_for_parse_count": int(result.get("messages_for_parse_count", 0)),
                "parsed_records_count": int(result.get("parsed_records_count", 0)),
                "written_records_count": int(result.get("written_records_count", 0)),
                "json_written_bills": int(result.get("json_written_bills", 0)),
                "data_path": str(result.get("data_path", "")),
                "json_path": str(result.get("json_path", "")),
                "log_path": str(log_path),
                "records_preview": preview_records,
            }
            if remote_normalization:
                response_payload["normalization"] = _to_json_safe(remote_normalization)

            audit_enabled = _to_bool(
                config.get("dify_audit_enabled", DEFAULT_DIFY_AUDIT_ENABLED),
                DEFAULT_DIFY_AUDIT_ENABLED,
            )
            if audit_enabled:
                audit_path = Path(str(config.get("dify_audit_path", DEFAULT_DIFY_AUDIT_PATH)))
                try:
                    audit_payload = {
                        "recorded_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "request": payload,
                        "route": route_info,
                        "response": {
                            "status": response_payload["status"],
                            "initialized_now": response_payload["initialized_now"],
                            "input_messages_count": response_payload["input_messages_count"],
                            "parsed_records_count": response_payload["parsed_records_count"],
                            "written_records_count": response_payload["written_records_count"],
                            "json_written_bills": response_payload["json_written_bills"],
                            "data_path": response_payload["data_path"],
                            "json_path": response_payload["json_path"],
                            "log_path": response_payload["log_path"],
                        },
                    }
                    if remote_response:
                        audit_payload["remote_response"] = remote_response
                    _append_audit_record(audit_path, audit_payload)
                    response_payload["audit_path"] = str(audit_path)
                except Exception as exc:
                    trace(f"[DIFY] failed to write audit: {exc}")
                    response_payload["audit_error"] = str(exc)

            self._write_json(HTTPStatus.OK, response_payload)
        except Exception as exc:
            trace(f"[DIFY] processing failed: {exc}")
            trace(traceback.format_exc())
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {
                    "ok": False,
                    "error": f"processing failed: {exc}",
                    "log_path": str(log_path),
                },
            )


def run_server() -> None:
    host = DEFAULT_DIFY_API_HOST
    port = DEFAULT_DIFY_API_PORT
    ingest_path = DEFAULT_DIFY_INGEST_PATH
    health_path = "/health"
    try:
        config, _ = load_runtime_config()
        host = _safe_text(config.get("dify_api_host", host)) or host
        port = _to_int(config.get("dify_api_port", port), port, minimum=1)
        ingest_path = _normalize_path(str(config.get("dify_ingest_path", ingest_path)), DEFAULT_DIFY_INGEST_PATH)
        health_path = _normalize_path(str(config.get("dify_health_path", health_path)), "/health")
    except Exception:
        pass

    server = _ReusableThreadingHTTPServer((host, port), DifyBridgeHandler)
    server.ingest_path = ingest_path
    server.health_paths = {"/", health_path, "/health", "/api/health"}
    print(f"[DIFY] {SERVICE_NAME} listening on http://{host}:{port}")
    print(f"[DIFY] POST {ingest_path}")
    print(f"[DIFY] GET  {health_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("[DIFY] server stopped.")


if __name__ == "__main__":
    run_server()
