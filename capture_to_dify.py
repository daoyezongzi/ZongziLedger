from __future__ import annotations

import json
import sys
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.constants import (
    DEFAULT_CAPTURE_BACKEND,
    DEFAULT_CAPTURE_MODE,
    DEFAULT_CONFIG_PATH,
    DEFAULT_DIFY_API_HOST,
    DEFAULT_DIFY_API_PORT,
    DEFAULT_DIFY_INGEST_PATH,
    DEFAULT_DIFY_INGEST_TIMEOUT_SECONDS,
    DEFAULT_DIFY_INGEST_URL,
    DEFAULT_DIFY_SOURCE_ID,
    DEFAULT_DOTENV_PATH,
    DEFAULT_MAX_MESSAGES,
    resolve_start_marker,
)
from core.monitor import MonitorError
from main import (
    _build_manual_messages,
    _fetch_messages_auto,
    _is_admin,
    _make_trace,
    _sanitize_csv_text,
    _to_int,
    init_run_logger,
    load_runtime_config,
)
from setup import check_environment


def _safe_text(value: Any) -> str:
    return str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def _json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _resolve_ingest_url(config: Dict[str, Any]) -> str:
    explicit_url = _safe_text(config.get("dify_ingest_url", DEFAULT_DIFY_INGEST_URL))
    if explicit_url:
        return explicit_url

    host = _safe_text(config.get("dify_api_host", DEFAULT_DIFY_API_HOST)) or DEFAULT_DIFY_API_HOST
    port = _to_int(config.get("dify_api_port", DEFAULT_DIFY_API_PORT), DEFAULT_DIFY_API_PORT, minimum=1)
    path = _safe_text(config.get("dify_ingest_path", DEFAULT_DIFY_INGEST_PATH)) or DEFAULT_DIFY_INGEST_PATH
    if not path.startswith("/"):
        path = f"/{path}"

    if host.startswith("http://") or host.startswith("https://"):
        return f"{host.rstrip('/')}{path}"
    return f"http://{host}:{port}{path}"


def _normalize_capture_messages(
    raw_messages: List[Dict[str, str]],
    default_source_id: str,
) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_id = _safe_text(default_source_id) or DEFAULT_DIFY_SOURCE_ID

    for message in raw_messages:
        if not isinstance(message, dict):
            continue
        raw_text = _sanitize_csv_text(message.get("message", ""))
        if not raw_text:
            continue

        row: Dict[str, str] = {
            "message": raw_text,
            "timestamp": _safe_text(message.get("timestamp", now_text)) or now_text,
            "source_id": _safe_text(message.get("source_id", source_id)) or source_id,
        }

        message_hash = _safe_text(message.get("message_hash", ""))
        if message_hash:
            row["message_hash"] = message_hash

        captured_at = _safe_text(message.get("message_captured_at", ""))
        if captured_at:
            row["message_captured_at"] = captured_at

        normalized.append(row)

    return normalized


def _build_ingest_payload(raw_messages: List[Dict[str, str]], config: Dict[str, Any]) -> Dict[str, Any]:
    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    source_id = _safe_text(config.get("dify_source_id", DEFAULT_DIFY_SOURCE_ID)) or DEFAULT_DIFY_SOURCE_ID
    capture_mode = _safe_text(config.get("capture_mode", DEFAULT_CAPTURE_MODE)) or DEFAULT_CAPTURE_MODE
    capture_backend = _safe_text(config.get("capture_backend", DEFAULT_CAPTURE_BACKEND)) or DEFAULT_CAPTURE_BACKEND

    messages = _normalize_capture_messages(raw_messages, default_source_id=source_id)
    return {
        "source_id": source_id,
        "timestamp": now_text,
        "messages": messages,
        "capture_meta": {
            "capture_mode": capture_mode,
            "capture_backend": capture_backend,
            "sent_at": now_text,
            "message_count": len(messages),
        },
    }


def _post_json(url: str, payload: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    req = urllib.request.Request(
        url=url,
        data=_json_compact(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=max(1, int(timeout_seconds))) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"request failed: {exc}") from exc

    try:
        parsed = json.loads(body_text)
    except Exception as exc:
        raise RuntimeError(f"response is not valid JSON: {body_text[:500]}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("response root must be JSON object")
    return parsed


def _print_response_summary(response: Dict[str, Any]) -> None:
    route = response.get("route", {})
    mode = ""
    if isinstance(route, dict):
        mode = _safe_text(route.get("mode", ""))

    print(
        f"[DIFY-PUSH] status={_safe_text(response.get('status', '')) or '-'} "
        f"route={mode or '-'} "
        f"written_records={_to_int(response.get('written_records_count', 0), 0, minimum=0)} "
        f"json_bills={_to_int(response.get('json_written_bills', 0), 0, minimum=0)}"
    )
    data_path = _safe_text(response.get("data_path", ""))
    json_path = _safe_text(response.get("json_path", ""))
    log_path = _safe_text(response.get("log_path", ""))
    if data_path:
        print(f"[DIFY-PUSH] data_path={data_path}")
    if json_path:
        print(f"[DIFY-PUSH] json_path={json_path}")
    if log_path:
        print(f"[DIFY-PUSH] log_path={log_path}")


def _capture_messages(config: Dict[str, Any], trace) -> List[Dict[str, str]]:
    capture_mode = _safe_text(config.get("capture_mode", DEFAULT_CAPTURE_MODE)).lower() or DEFAULT_CAPTURE_MODE
    if capture_mode == "manual":
        print("[模式] 当前为手动录入模式（capture_mode=manual）。")
        return _build_manual_messages(resolve_start_marker(config))

    raw_messages = _fetch_messages_auto(config, trace=trace)
    inspected_count = _to_int(config.get("max_messages", DEFAULT_MAX_MESSAGES), DEFAULT_MAX_MESSAGES, minimum=1)
    start_marker = resolve_start_marker(config)
    print(f"[抓取] 已检查最近 {inspected_count} 条消息，前缀 {start_marker} 命中 {len(raw_messages)} 条。")
    return raw_messages


def run() -> int:
    print("[启动] capture_to_dify 开始执行。")
    if not check_environment():
        return 1

    try:
        config, dotenv_applied = load_runtime_config(DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH)
    except Exception as exc:
        print(f"[错误] 读取配置失败：{exc}")
        return 1

    logger, log_path = init_run_logger(config)
    trace = _make_trace(logger)
    trace(f"[PUSH] 启动时间={datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    trace(f"[PUSH] Python={sys.version}")
    trace(f"[PUSH] 管理员权限={'是' if _is_admin() else '否'}")
    if dotenv_applied > 0:
        trace(f"[PUSH] 已加载 .env 覆盖项数量={dotenv_applied}")

    try:
        raw_messages = _capture_messages(config, trace=trace)
    except MonitorError as exc:
        trace(f"[PUSH] 抓取失败: {exc}")
        print(f"[错误] 抓取消息失败：{exc}")
        print(f"[提示] 详细日志：{log_path}")
        return 1
    except Exception as exc:
        trace(f"[PUSH] 抓取过程异常: {exc}")
        trace(traceback.format_exc())
        print(f"[错误] 抓取过程发生异常：{exc}")
        print(f"[提示] 详细日志：{log_path}")
        return 1

    if not raw_messages:
        print("[提示] 本次未抓到可推送消息。")
        print(f"[提示] 详细日志：{log_path}")
        return 0

    payload = _build_ingest_payload(raw_messages, config)
    payload_messages = payload.get("messages", [])
    if not isinstance(payload_messages, list) or not payload_messages:
        print("[提示] 本次消息为空，未执行推送。")
        print(f"[提示] 详细日志：{log_path}")
        return 0

    ingest_url = _resolve_ingest_url(config)
    timeout_seconds = _to_int(
        config.get("dify_ingest_timeout_seconds", DEFAULT_DIFY_INGEST_TIMEOUT_SECONDS),
        DEFAULT_DIFY_INGEST_TIMEOUT_SECONDS,
        minimum=1,
    )
    trace(
        f"[PUSH] POST {ingest_url}, timeout={timeout_seconds}s, messages={len(payload_messages)}"
    )

    try:
        response = _post_json(ingest_url, payload, timeout_seconds=timeout_seconds)
    except Exception as exc:
        trace(f"[PUSH] 推送失败: {exc}")
        print(f"[错误] 推送到 Dify bridge 失败：{exc}")
        print(f"[提示] 请先确认 bridge 已启动并监听：{ingest_url}")
        print(f"[提示] 详细日志：{log_path}")
        return 1

    if not bool(response.get("ok", False)):
        err = _safe_text(response.get("error", "unknown error"))
        trace(f"[PUSH] bridge 返回失败: {err}")
        print(f"[错误] bridge 返回失败：{err}")
        _print_response_summary(response)
        return 1

    print(f"[DIFY-PUSH] 推送成功，messages={len(payload_messages)}")
    _print_response_summary(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
