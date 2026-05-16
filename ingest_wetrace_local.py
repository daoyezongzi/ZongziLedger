from __future__ import annotations

import hashlib
import json
import traceback
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib import error as url_error
from urllib import request as url_request

from core.constants import DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH
from core.wetrace_client import WetraceClient, WetraceClientConfig, WetraceClientError
from core.wetrace_state import WetraceState, load_wetrace_state, save_wetrace_state
from main import _make_trace, init_run_logger, load_runtime_config


def _to_text(value: Any) -> str:
    return str(value or "").strip()


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = _to_text(value).lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _to_int(value: Any, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    if minimum is not None and parsed < minimum:
        parsed = minimum
    if maximum is not None and parsed > maximum:
        parsed = maximum
    return parsed


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _extract_first(payload: Dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = payload.get(key)
        text = _to_text(value)
        if text:
            return text
    return ""


def _extract_int(payload: Dict[str, Any], keys: Iterable[str]) -> int:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text = _to_text(value)
        if not text:
            continue
        try:
            return int(text)
        except Exception:
            continue
    return 0


def _format_timestamp(payload: Dict[str, Any]) -> str:
    text_candidate = _extract_first(
        payload,
        ("time", "timestamp", "create_time_text", "createTimeText", "message_time", "msg_time"),
    )
    if text_candidate:
        return text_candidate

    epoch = _extract_int(
        payload,
        ("create_time", "createTime", "timestamp_unix", "timestamp_epoch", "msg_create_time"),
    )
    if epoch > 0:
        if epoch > 10**12:
            epoch = epoch // 1000
        try:
            return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    return _now_text()


def _parse_allowed_types(config: Dict[str, Any]) -> Sequence[int]:
    raw = config.get("wetrace_text_type_values", "1")
    if isinstance(raw, (list, tuple, set)):
        values = list(raw)
    else:
        values = _to_text(raw).split(",")

    parsed: List[int] = []
    for item in values:
        text = _to_text(item)
        if not text:
            continue
        try:
            parsed.append(int(text))
        except Exception:
            continue
    return parsed or [1]


def _resolve_talker_id(
    client: WetraceClient,
    *,
    explicit_talker_id: str,
    talker_keyword: str,
    trace,
    talker_limit: int,
) -> Tuple[str, str]:
    if explicit_talker_id:
        return explicit_talker_id, ""
    if not talker_keyword:
        return "", ""

    rows = client.list_chatrooms(keyword=talker_keyword, limit=talker_limit, offset=0)
    if not rows:
        return "", ""

    chosen = rows[0] if isinstance(rows[0], dict) else {}
    talker_id = _extract_first(chosen, ("talker_id", "talker", "talkerId", "chat_id", "id", "username"))
    talker_name = _extract_first(chosen, ("talker_name", "talkerName", "name", "nickname", "display_name"))

    if trace:
        trace(
            f"[WETRACE] talker resolve keyword={talker_keyword}, matches={len(rows)}, "
            f"chosen_id={talker_id or '<empty>'}, chosen_name={talker_name or '<empty>'}"
        )

    return talker_id, talker_name


def _normalize_wetrace_message(
    payload: Dict[str, Any],
    *,
    talker_id: str,
    default_chat_name: str,
    last_seq: int,
    keyword: str,
    keyword_case_sensitive: bool,
    text_only: bool,
    allowed_types: Sequence[int],
    captured_at: str,
) -> Tuple[Optional[Dict[str, Any]], int, str]:
    content = _extract_first(payload, ("content", "message", "text", "body"))
    if not content:
        return None, 0, "missing_content"

    talker = _extract_first(payload, ("talker_id", "talker", "talkerId", "chat_id", "conversation_id")) or talker_id
    if talker_id and talker and talker != talker_id:
        return None, 0, "talker_mismatch"

    seq = _extract_int(payload, ("seq", "local_id", "localId", "id"))
    if seq > 0 and last_seq > 0 and seq <= last_seq:
        return None, seq, "old_seq"

    msg_type = _extract_int(payload, ("type", "msg_type", "msgType"))
    if text_only and msg_type > 0 and msg_type not in set(allowed_types):
        return None, seq, "non_text_type"

    if keyword:
        haystack = content if keyword_case_sensitive else content.lower()
        needle = keyword if keyword_case_sensitive else keyword.lower()
        if needle not in haystack:
            return None, seq, "keyword_miss"

    talker_name = _extract_first(payload, ("talker_name", "talkerName", "chat_name", "conversation_name"))
    if not talker_name:
        talker_name = default_chat_name
    sender_name = _extract_first(payload, ("sender_name", "senderName", "sender", "nickname", "display_name", "name"))
    sender_id = _extract_first(payload, ("sender_id", "senderId", "from_user"))
    msg_svr_id = _extract_first(payload, ("msg_svr_id", "msgSvrId", "server_id"))

    source_id = f"wetrace:{talker or talker_id}"
    identity_text = f"{talker or talker_id}|{seq}|{content}"
    message_hash = hashlib.sha1(identity_text.encode("utf-8", errors="ignore")).hexdigest()

    source_ref = ""
    if seq > 0:
        source_ref = f"wetrace_seq:{seq}"
    elif msg_svr_id:
        source_ref = f"wetrace_msg:{msg_svr_id}"

    normalized: Dict[str, Any] = {
        "message": content,
        "timestamp": _format_timestamp(payload),
        "source_id": source_id,
        "message_hash": message_hash,
        "message_captured_at": captured_at,
        "source_type": "wetrace",
        "source_ref": source_ref,
        "chat_id": talker or talker_id,
        "chat_name": talker_name,
        "sender": sender_name,
        "name": sender_name,
        "sender_id": sender_id,
        "wetrace_seq": str(seq) if seq > 0 else "",
        "wetrace_msg_svr_id": msg_svr_id,
    }
    return normalized, seq, "ok"


def _build_fetch_query(
    talker_id: str,
    *,
    keyword: str,
    keyword_server_side: bool,
    limit: int,
    offset: int,
    last_seq: int,
    reverse: bool,
) -> Dict[str, Any]:
    query: Dict[str, Any] = {
        "talker_id": talker_id,
        "limit": limit,
        "offset": offset,
        "reverse": reverse,
    }
    if keyword and keyword_server_side:
        query["keyword"] = keyword
    if last_seq > 0:
        query["after"] = last_seq
    return query


def _resolve_dify_ingest_url(config: Dict[str, Any]) -> str:
    explicit_url = _to_text(config.get("wetrace_dify_ingest_url", ""))
    if explicit_url:
        return explicit_url

    dify_ingest_url = _to_text(config.get("dify_ingest_url", ""))
    if dify_ingest_url:
        return dify_ingest_url

    host = _to_text(config.get("dify_api_host", "127.0.0.1")) or "127.0.0.1"
    port = _to_int(config.get("dify_api_port", 8787), 8787, minimum=1, maximum=65535)
    path = _to_text(config.get("dify_ingest_path", "/api/dify/ingest")) or "/api/dify/ingest"
    if not path.startswith("/"):
        path = "/" + path
    return f"http://{host}:{port}{path}"


def _post_json(url: str, payload: Dict[str, Any], timeout_seconds: int) -> Dict[str, Any]:
    req = url_request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with url_request.urlopen(req, timeout=max(1, int(timeout_seconds))) as resp:
            body_text = resp.read().decode("utf-8", errors="replace")
    except url_error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {body_text[:500]}") from exc
    except url_error.URLError as exc:
        raise RuntimeError(f"request failed: {exc}") from exc

    try:
        parsed = json.loads(body_text) if body_text else {}
    except Exception as exc:
        raise RuntimeError(f"response is not valid JSON: {body_text[:500]}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError("response root must be JSON object")
    return parsed


def run() -> int:
    print("[START] wetrace local ingest started")
    try:
        config, dotenv_applied = load_runtime_config(DEFAULT_CONFIG_PATH, DEFAULT_DOTENV_PATH)
    except Exception as exc:
        print(f"[ERROR] failed to load config: {exc}")
        return 1

    logger, log_path = init_run_logger(config)
    trace = _make_trace(logger)
    trace(f"[WETRACE] start_time={_now_text()}")
    if dotenv_applied > 0:
        trace(f"[WETRACE] loaded .env overrides={dotenv_applied}")

    base_url = _to_text(config.get("wetrace_base_url", ""))
    talker_id = _to_text(config.get("wetrace_talker_id", ""))
    talker_keyword = _to_text(config.get("wetrace_talker_keyword", ""))
    talker_limit = _to_int(config.get("wetrace_talker_limit", 20), 20, minimum=1, maximum=200)
    auth_token = _to_text(config.get("wetrace_auth_token", ""))
    timeout_seconds = _to_int(config.get("wetrace_timeout_seconds", 20), 20, minimum=1, maximum=120)
    limit = _to_int(config.get("wetrace_limit", 100), 100, minimum=1, maximum=500)
    max_pages = _to_int(config.get("wetrace_max_pages", 5), 5, minimum=1, maximum=50)
    reverse = _to_bool(config.get("wetrace_reverse", True), True)
    text_only = _to_bool(config.get("wetrace_text_only", True), True)
    keyword = _to_text(config.get("wetrace_keyword", ""))
    keyword_case_sensitive = _to_bool(config.get("wetrace_keyword_case_sensitive", False), False)
    keyword_server_side = _to_bool(config.get("wetrace_keyword_server_side", False), False)
    trigger_sync = _to_bool(config.get("wetrace_trigger_sync", False), False)
    sync_fail_closed = _to_bool(config.get("wetrace_sync_fail_closed", False), False)
    dispatch_mode = _to_text(config.get("wetrace_dispatch_mode", "local")).lower() or "local"
    if dispatch_mode not in {"dify", "workflow", "bridge"}:
        trace(f"[WETRACE] unsupported wetrace_dispatch_mode={dispatch_mode}, require dify/workflow/bridge")
        print(f"[ERROR] unsupported wetrace_dispatch_mode={dispatch_mode}, require dify/workflow/bridge")
        print(f"[LOG] {log_path}")
        return 1
    dify_timeout_seconds = _to_int(
        config.get("wetrace_dify_timeout_seconds", config.get("dify_ingest_timeout_seconds", 30)),
        30,
        minimum=1,
        maximum=300,
    )
    allowed_types = _parse_allowed_types(config)
    state_path = Path(str(config.get("wetrace_state_path", "data/wetrace_state.local.json")))
    chatrooms_path = _to_text(config.get("wetrace_chatrooms_path", "/api/v1/chatrooms")) or "/api/v1/chatrooms"
    messages_path = _to_text(config.get("wetrace_messages_path", "/api/v1/messages")) or "/api/v1/messages"
    sync_path = _to_text(config.get("wetrace_sync_path", "/api/v1/system/sync")) or "/api/v1/system/sync"

    if not base_url:
        trace("[WETRACE] missing config: wetrace_base_url")
        print("[ERROR] missing wetrace_base_url")
        print(f"[LOG] {log_path}")
        return 1
    client = WetraceClient(
        WetraceClientConfig(
            base_url=base_url,
            auth_token=auth_token,
            timeout_seconds=timeout_seconds,
            chatrooms_path=chatrooms_path,
            messages_path=messages_path,
            sync_path=sync_path,
        )
    )

    resolved_talker_name = ""
    try:
        talker_id, resolved_talker_name = _resolve_talker_id(
            client,
            explicit_talker_id=talker_id,
            talker_keyword=talker_keyword,
            trace=trace,
            talker_limit=talker_limit,
        )
    except Exception as exc:
        trace(f"[WETRACE] failed to resolve talker by keyword: {exc}")
        print(f"[ERROR] failed to resolve talker: {exc}")
        print(f"[LOG] {log_path}")
        return 1

    if not talker_id:
        trace("[WETRACE] missing target talker, set wetrace_talker_id or wetrace_talker_keyword")
        print("[ERROR] missing target talker, configure wetrace_talker_id or wetrace_talker_keyword")
        print(f"[LOG] {log_path}")
        return 1

    state = load_wetrace_state(state_path)
    last_seq = max(0, int(state.last_seq or 0))
    trace(
        f"[WETRACE] config: base_url={base_url}, talker_id={talker_id}, talker_name={resolved_talker_name or '<empty>'}, "
        f"limit={limit}, max_pages={max_pages}, text_only={text_only}, keyword={'<empty>' if not keyword else keyword}, "
        f"dispatch_mode={dispatch_mode}, last_seq={last_seq}"
    )

    if trigger_sync:
        try:
            sync_result = client.trigger_sync()
            trace(f"[WETRACE] sync triggered: {json.dumps(sync_result, ensure_ascii=False)}")
        except Exception as exc:
            trace(f"[WETRACE] sync failed: {exc}")
            if sync_fail_closed:
                print(f"[ERROR] wetrace sync failed: {exc}")
                print(f"[LOG] {log_path}")
                return 1

    raw_rows: List[Dict[str, Any]] = []
    try:
        for page in range(max_pages):
            offset = page * limit
            query = _build_fetch_query(
                talker_id,
                keyword=keyword,
                keyword_server_side=keyword_server_side,
                limit=limit,
                offset=offset,
                last_seq=last_seq,
                reverse=reverse,
            )
            rows = client.list_messages(query)
            trace(f"[WETRACE] page={page + 1}, offset={offset}, fetched={len(rows)}")
            if not rows:
                break
            raw_rows.extend(rows)
            if len(rows) < limit:
                break
    except WetraceClientError as exc:
        trace(f"[WETRACE] fetch failed: {exc}")
        print(f"[ERROR] wetrace fetch failed: {exc}")
        print(f"[LOG] {log_path}")
        return 1
    except Exception as exc:
        trace(f"[WETRACE] unexpected fetch error: {exc}")
        trace(traceback.format_exc())
        print(f"[ERROR] unexpected fetch error: {exc}")
        print(f"[LOG] {log_path}")
        return 1

    captured_at = _now_text()
    normalized_messages: List[Tuple[int, int, Dict[str, Any]]] = []
    reason_counts: Counter[str] = Counter()
    dedupe_keys = set()
    raw_max_seq = last_seq
    for idx, row in enumerate(raw_rows):
        if not isinstance(row, dict):
            reason_counts["not_dict"] += 1
            continue

        seq_any = _extract_int(row, ("seq", "local_id", "localId", "id"))
        if seq_any > raw_max_seq:
            raw_max_seq = seq_any

        normalized, seq, reason = _normalize_wetrace_message(
            row,
            talker_id=talker_id,
            default_chat_name=resolved_talker_name,
            last_seq=last_seq,
            keyword=keyword,
            keyword_case_sensitive=keyword_case_sensitive,
            text_only=text_only,
            allowed_types=allowed_types,
            captured_at=captured_at,
        )
        if reason != "ok" or normalized is None:
            reason_counts[reason] += 1
            continue

        talker_key = _to_text(normalized.get("chat_id", ""))
        if seq > 0:
            dedupe_key = ("seq", talker_key, str(seq))
        else:
            dedupe_key = (
                "fallback",
                talker_key,
                _to_text(normalized.get("wetrace_msg_svr_id", "")) or _to_text(normalized.get("message_hash", "")),
            )
        if dedupe_key in dedupe_keys:
            reason_counts["duplicate"] += 1
            continue
        dedupe_keys.add(dedupe_key)
        normalized_messages.append((seq, idx, normalized))

    normalized_messages.sort(key=lambda item: (item[0] <= 0, item[0], item[1]))
    capture_messages = [item[2] for item in normalized_messages]

    if capture_messages:
        trace(f"[WETRACE] normalized messages={len(capture_messages)}")
    else:
        trace("[WETRACE] no messages matched ingest rules")

    ledger_result: Dict[str, Any] = {"written_records_count": 0, "json_written_bills": 0, "status": "no_data"}
    try:
        if capture_messages:
            ingest_url = _resolve_dify_ingest_url(config)
            payload = {
                "schema_version": "zongziledger-wetrace-v1",
                "source_id": _to_text(config.get("dify_source_id", "wetrace")) or "wetrace",
                "timestamp": _now_text(),
                "messages": capture_messages,
                "capture_meta": {
                    "capture_mode": "wetrace",
                    "capture_backend": "wetrace_api",
                    "talker_id": talker_id,
                    "talker_name": resolved_talker_name,
                    "message_count": len(capture_messages),
                },
            }
            trace(f"[WETRACE] POST workflow ingest: url={ingest_url}, messages={len(capture_messages)}")
            try:
                response = _post_json(ingest_url, payload, dify_timeout_seconds)
            except Exception as exc:
                err_text = _to_text(exc)
                if "no parseable ledger message" in err_text:
                    trace("[WETRACE] workflow returned no parseable ledger message, treated as no_data")
                    response = {
                        "ok": True,
                        "status": "workflow_no_data",
                        "written_records_count": 0,
                        "json_written_bills": 0,
                    }
                else:
                    raise
            ok_flag = True
            if "ok" in response:
                ok_flag = _to_bool(response.get("ok", True), True)
            elif "status" in response:
                status_text = _to_text(response.get("status", "")).lower()
                ok_flag = status_text not in {"error", "failed", "fail"}
            if not ok_flag:
                raise RuntimeError(f"workflow ingest returned failure: {json.dumps(response, ensure_ascii=False)[:500]}")

            ledger_result = dict(response)
            ledger_result.setdefault("written_records_count", 0)
            ledger_result.setdefault("json_written_bills", 0)
            ledger_result.setdefault("status", "workflow_ok")
            trace(
                f"[WETRACE] workflow ingest ok: written_records={ledger_result.get('written_records_count', 0)}, "
                f"json_bills={ledger_result.get('json_written_bills', 0)}"
            )
    except Exception as exc:
        trace(f"[WETRACE] ledger process failed: {exc}")
        trace(traceback.format_exc())
        print(f"[ERROR] ledger process failed: {exc}")
        print(f"[LOG] {log_path}")
        return 1

    try:
        new_last_seq = max(last_seq, raw_max_seq)
        save_wetrace_state(
            state_path,
            WetraceState(last_seq=new_last_seq, updated_at=_now_text()),
        )
        trace(f"[WETRACE] state updated: {state_path}, last_seq={new_last_seq}")
    except Exception as exc:
        trace(f"[WETRACE] failed to save state: {exc}")
        trace(traceback.format_exc())
        print(f"[ERROR] failed to save state: {exc}")
        print(f"[LOG] {log_path}")
        return 1

    print("[WETRACE-INGEST] summary")
    print(
        json.dumps(
            {
                "fetched": len(raw_rows),
                "matched": len(capture_messages),
                "dispatch_mode": dispatch_mode,
                "written_records_count": int(ledger_result.get("written_records_count", 0) or 0),
                "json_written_bills": int(ledger_result.get("json_written_bills", 0) or 0),
                "last_seq_before": last_seq,
                "last_seq_after": max(last_seq, raw_max_seq),
                "skipped": dict(reason_counts),
            },
            ensure_ascii=False,
        )
    )
    print(f"[LOG] {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
