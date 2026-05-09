from __future__ import annotations

import json
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
    DEFAULT_ORDER_ID_DIGITS,
    DEFAULT_ORDER_ID_REQUIRE_HASH,
    resolve_end_marker,
    resolve_start_marker,
)
from main import (
    _extract_order_id_from_message,
    init_run_logger,
    load_runtime_config,
    process_ledger_messages,
)


SERVICE_NAME = "zongziledger-dify-bridge"
MESSAGE_LIST_KEYS = ("messages", "ledger_messages", "records", "items")
MESSAGE_TEXT_KEYS = ("message", "normalized_block", "raw_text", "raw_message", "text", "content", "answer")
MESSAGE_NAME_KEYS = ("name", "customer_name", "buyer_name", "contact_name")


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


def _build_message_item(item: Any, default_source_id: str, default_timestamp: str) -> Optional[Dict[str, str]]:
    message_text = _extract_message_text(item)
    if not message_text:
        return None

    source_id = default_source_id
    timestamp = default_timestamp
    message_hash = ""
    message_captured_at = ""
    name = ""

    if isinstance(item, dict):
        source_id = _safe_text(item.get("source_id", default_source_id)) or default_source_id
        timestamp = _safe_text(item.get("timestamp", default_timestamp)) or default_timestamp
        message_hash = _safe_text(item.get("message_hash", ""))
        message_captured_at = _safe_text(item.get("message_captured_at", ""))
        for key in MESSAGE_NAME_KEYS:
            name = _safe_text(item.get(key, ""))
            if name:
                break

    output: Dict[str, str] = {
        "message": message_text,
        "source_id": source_id,
        "timestamp": timestamp,
    }
    if message_hash:
        output["message_hash"] = message_hash
    if message_captured_at:
        output["message_captured_at"] = message_captured_at
    if name:
        output["name"] = name
    return output


def build_messages_from_payload(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    default_source_id = _safe_text(payload.get("source_id", "dify")) or "dify"
    default_timestamp = _safe_text(payload.get("timestamp", now_text)) or now_text

    messages: List[Dict[str, str]] = []
    raw_messages = payload.get("messages")
    if isinstance(raw_messages, list):
        for item in raw_messages:
            message_item = _build_message_item(
                item,
                default_source_id=default_source_id,
                default_timestamp=default_timestamp,
            )
            if message_item is not None:
                messages.append(message_item)
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
        if text[:1] in {"{", "["}:
            try:
                return _extract_remote_payload(json.loads(text))
            except Exception:
                pass
        return {"message": text}

    if isinstance(candidate, list):
        return {"messages": candidate}

    if not isinstance(candidate, dict):
        return {}

    for key in MESSAGE_LIST_KEYS:
        value = candidate.get(key)
        if isinstance(value, list):
            return {"messages": value}

    for key in MESSAGE_TEXT_KEYS:
        value = candidate.get(key)
        if isinstance(value, str) and _safe_text(value):
            return {"message": _safe_text(value)}

    for nested_key in ("result", "output", "payload", "data"):
        nested = candidate.get(nested_key)
        nested_payload = _extract_remote_payload(nested)
        if nested_payload:
            return nested_payload

    outputs = candidate.get("outputs")
    nested_outputs = _extract_remote_payload(outputs)
    if nested_outputs:
        return nested_outputs

    return {}


def _resolve_remote_messages(remote_response: Dict[str, Any]) -> List[Dict[str, str]]:
    payload = _extract_remote_payload(remote_response)
    if not payload:
        data = remote_response.get("data")
        if isinstance(data, dict):
            payload = _extract_remote_payload(data)
    if not payload:
        return []
    return build_messages_from_payload(payload)


def _is_valid_ledger_block_message(
    message_text: str,
    start_marker: str,
    end_marker: str,
    order_id_digits: int,
    require_hash: bool,
) -> bool:
    text = _safe_text(message_text)
    if not text:
        return False
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if len(lines) < 3:
        return False
    if lines[-1] != end_marker:
        return False
    order_id = _extract_order_id_from_message(
        text,
        start_marker=start_marker,
        order_id_digits=order_id_digits,
        require_hash=require_hash,
    )
    return bool(order_id)


def _filter_remote_messages_for_ledger(
    messages: List[Dict[str, str]],
    config: Dict[str, Any],
) -> Tuple[List[Dict[str, str]], int]:
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config, fallback=DEFAULT_END_MARKER)
    order_id_digits = _to_int(
        config.get("order_id_digits", DEFAULT_ORDER_ID_DIGITS),
        DEFAULT_ORDER_ID_DIGITS,
        minimum=1,
    )
    require_hash = _to_bool(
        config.get("order_id_require_hash", DEFAULT_ORDER_ID_REQUIRE_HASH),
        DEFAULT_ORDER_ID_REQUIRE_HASH,
    )

    valid_messages: List[Dict[str, str]] = []
    invalid_count = 0
    for message in messages:
        raw = _safe_text(message.get("message", "")) if isinstance(message, dict) else ""
        if _is_valid_ledger_block_message(
            raw,
            start_marker=start_marker,
            end_marker=end_marker,
            order_id_digits=order_id_digits,
            require_hash=require_hash,
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

    normalized_messages = build_messages_from_payload(payload)
    first_message_text = normalized_messages[0]["message"] if normalized_messages else ""

    inputs: Dict[str, Any] = {}
    if as_json_text:
        inputs[payload_key] = _json_compact(payload)
        inputs[messages_key] = _json_compact(normalized_messages)
    else:
        inputs[payload_key] = payload
        inputs[messages_key] = normalized_messages

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

            selected_messages: List[Dict[str, str]] = []
            remote_error = ""
            route = "local"
            remote_response: Dict[str, Any] = {}

            if remote_enabled:
                try:
                    remote_response = _call_remote_dify(payload, config)
                    selected_messages = _resolve_remote_messages(remote_response)
                    if selected_messages:
                        raw_remote_count = len(selected_messages)
                        filtered_remote_messages, invalid_remote_messages = _filter_remote_messages_for_ledger(
                            selected_messages,
                            config,
                        )
                        if filtered_remote_messages:
                            selected_messages = filtered_remote_messages
                            route = "remote"
                            if invalid_remote_messages > 0:
                                trace(
                                    f"[DIFY] remote messages filtered: kept={len(filtered_remote_messages)}, dropped={invalid_remote_messages}"
                                )
                        else:
                            selected_messages = []
                            remote_error = (
                                f"remote response has no valid ledger block message (raw_messages={raw_remote_count})"
                            )
                            trace(
                                "[DIFY] remote response resolved messages but none passed ledger-block validation"
                            )
                    else:
                        remote_error = "remote response has no parseable ledger message"
                        trace(f"[DIFY] remote returned empty messages")
                except Exception as exc:
                    remote_error = str(exc)
                    trace(f"[DIFY] remote call failed: {exc}")

                if route != "remote" and remote_priority and not remote_fallback_local:
                    self._write_json(
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "ok": False,
                            "error": f"remote-priority failed: {remote_error or 'unknown remote error'}",
                            "service": SERVICE_NAME,
                            "log_path": str(log_path),
                        },
                    )
                    return

            if not selected_messages:
                selected_messages = build_messages_from_payload(payload)
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
                f"[DIFY] ingest route={route}, messages={len(selected_messages)}, dotenv_applied={dotenv_applied}"
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

            response_payload: Dict[str, Any] = {
                "ok": True,
                "service": SERVICE_NAME,
                "workflow": "dify",
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
