"""Shared message contract normalization for capture and Dify pipelines."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Mapping, Sequence, Tuple

CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

RAW_REQUIRED_FIELDS: Tuple[str, ...] = ("message", "timestamp", "source_id")
NORMALIZED_REQUIRED_FIELDS: Tuple[str, ...] = (
    "message",
    "timestamp",
    "source_id",
    "message_hash",
    "message_captured_at",
)

MESSAGE_TEXT_KEYS: Tuple[str, ...] = (
    "message",
    "raw_message",
    "normalized_block",
    "cleaned_message",
    "content",
    "text",
    "body",
)
TIMESTAMP_KEYS: Tuple[str, ...] = (
    "timestamp",
    "visible_time",
    "message_time",
    "created_at",
    "event_time",
    "time",
)
SOURCE_ID_KEYS: Tuple[str, ...] = (
    "source_id",
    "chat_id",
    "chat_name",
    "source",
    "conversation_id",
    "source_scope_id",
)
MESSAGE_HASH_KEYS: Tuple[str, ...] = ("message_hash", "hash")
CAPTURED_AT_KEYS: Tuple[str, ...] = ("message_captured_at", "captured_at")
NAME_KEYS: Tuple[str, ...] = ("name", "sender", "customer_name", "buyer_name", "contact_name")
CHAT_ID_KEYS: Tuple[str, ...] = ("chat_id", "conversation_id")
CHAT_NAME_KEYS: Tuple[str, ...] = ("chat_name",)

RESERVED_KEYS = set(
    MESSAGE_TEXT_KEYS
    + TIMESTAMP_KEYS
    + SOURCE_ID_KEYS
    + MESSAGE_HASH_KEYS
    + CAPTURED_AT_KEYS
    + NAME_KEYS
    + CHAT_ID_KEYS
    + CHAT_NAME_KEYS
    + ("normalize_hint", "raw_context", "visible_time", "visible_time_reason", "raw_message_original")
)


def _sanitize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_TEXT_RE.sub("", text)
    return text.strip()


def _to_json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_to_json_safe(v) for v in value]
    return str(value)


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fingerprint_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _pick_text(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            text = _sanitize_text(value)
            if text:
                return text
            continue
        if isinstance(value, (int, float, bool)):
            return _sanitize_text(str(value))
        if isinstance(value, list):
            chunks = [_sanitize_text(x) for x in value if _sanitize_text(x)]
            if chunks:
                return "\n".join(chunks)
    return ""


def _as_mapping(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return dict(item)
    if isinstance(item, str):
        return {"message": item}
    return {}


def _build_contract_issue(
    index: int,
    reason: str,
    source: str,
    entry: Any = None,
    normalized: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    issue: Dict[str, Any] = {
        "index": int(index),
        "reason": _sanitize_text(reason),
        "source": _sanitize_text(source),
        "entry_type": type(entry).__name__ if entry is not None else "",
    }
    if isinstance(entry, (dict, str)):
        issue["entry_preview"] = _sanitize_text(entry)[:240]
    if normalized is not None:
        issue["normalized_preview"] = _sanitize_text(normalized.get("message", ""))[:240]
        issue["source_id"] = _sanitize_text(normalized.get("source_id", ""))
        issue["timestamp"] = _sanitize_text(normalized.get("timestamp", ""))
    return issue


def _normalize_message_entry(
    entry: Mapping[str, Any],
    *,
    default_source_id: str,
    default_now_text: str,
) -> Tuple[Dict[str, Any], str]:
    message = _pick_text(entry, MESSAGE_TEXT_KEYS)
    if not message:
        return {}, "missing_message"

    timestamp = _pick_text(entry, TIMESTAMP_KEYS) or default_now_text
    source_id = _pick_text(entry, SOURCE_ID_KEYS) or _sanitize_text(default_source_id) or "local"
    message_hash = _pick_text(entry, MESSAGE_HASH_KEYS) or _fingerprint_text(message)
    message_captured_at = _pick_text(entry, CAPTURED_AT_KEYS) or default_now_text

    normalized: Dict[str, Any] = {
        "message": message,
        "raw_message": _pick_text(entry, ("raw_message",)) or message,
        "timestamp": timestamp,
        "source_id": source_id,
        "message_hash": message_hash,
        "message_captured_at": message_captured_at,
    }

    name_text = _pick_text(entry, NAME_KEYS)
    if name_text:
        normalized["name"] = name_text
    sender = _pick_text(entry, ("sender",))
    if sender:
        normalized["sender"] = sender
    elif name_text:
        normalized["sender"] = name_text

    chat_id = _pick_text(entry, CHAT_ID_KEYS)
    if chat_id:
        normalized["chat_id"] = chat_id
    chat_name = _pick_text(entry, CHAT_NAME_KEYS)
    if chat_name:
        normalized["chat_name"] = chat_name

    visible_time = _pick_text(entry, ("visible_time",))
    if visible_time:
        normalized["visible_time"] = visible_time
    visible_time_reason = _pick_text(entry, ("visible_time_reason",))
    if visible_time_reason:
        normalized["visible_time_reason"] = visible_time_reason

    normalize_hint = _pick_text(entry, ("normalize_hint",))
    if normalize_hint:
        normalized["normalize_hint"] = normalize_hint

    raw_context = entry.get("raw_context")
    if isinstance(raw_context, dict):
        normalized["raw_context"] = _to_json_safe(raw_context)
    elif raw_context is not None:
        normalized["raw_context"] = {"_raw_context": _to_json_safe(raw_context)}

    raw_original = _pick_text(entry, ("raw_message_original",))
    if raw_original:
        normalized["raw_message_original"] = raw_original

    for key, value in entry.items():
        if key in RESERVED_KEYS:
            continue
        if key in normalized:
            continue
        normalized[str(key)] = _to_json_safe(value)

    return normalized, ""


def ensure_raw_message_contract(
    messages: Sequence[Any],
    *,
    default_source_id: str = "capture",
    source: str = "raw",
    now_text: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    current_now_text = _sanitize_text(now_text) or _now_text()
    normalized_messages: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []

    for index, item in enumerate(messages):
        mapping = _as_mapping(item)
        if not mapping:
            issues.append(_build_contract_issue(index, "invalid_entry_type", source, entry=item))
            continue

        normalized, reason = _normalize_message_entry(
            mapping,
            default_source_id=default_source_id,
            default_now_text=current_now_text,
        )
        if reason:
            issues.append(_build_contract_issue(index, reason, source, entry=item))
            continue
        normalized_messages.append(normalized)

    stats = {
        "input_count": len(messages),
        "output_count": len(normalized_messages),
        "dropped_count": len(messages) - len(normalized_messages),
        "issue_count": len(issues),
    }
    return normalized_messages, issues, stats


def ensure_normalized_message_contract(
    messages: Sequence[Any],
    *,
    default_source_id: str = "dify",
    source: str = "normalized",
    now_text: str = "",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, int]]:
    normalized_messages, issues, stats = ensure_raw_message_contract(
        messages,
        default_source_id=default_source_id,
        source=source,
        now_text=now_text,
    )

    for idx, row in enumerate(normalized_messages):
        missing = [field for field in NORMALIZED_REQUIRED_FIELDS if not _sanitize_text(row.get(field, ""))]
        if not missing:
            continue
        issues.append(
            _build_contract_issue(
                idx,
                f"missing_required:{','.join(missing)}",
                source,
                entry=messages[idx] if idx < len(messages) else None,
                normalized=row,
            )
        )

    stats["required_issue_count"] = sum(
        1
        for item in issues
        if _sanitize_text(item.get("reason", "")).startswith("missing_required:")
    )
    return normalized_messages, issues, stats

