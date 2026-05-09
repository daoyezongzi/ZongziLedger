"""Parser for count-based ledger blocks between start/end markers."""

from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from core.constants import DEFAULT_END_MARKER, DEFAULT_START_MARKER

END_MARKER_DEFAULT = DEFAULT_END_MARKER

TRIM_EDGE_RE = re.compile(r"^[,，;；:：|/\\\-~.。\s]+|[,，;；:：|/\\\-~.。\s]+$")
SEPARATOR_ONLY_RE = re.compile(r"^[,，;；:：|/\\\-~.。\s]+$")
COUNT_LINE_RE = re.compile(
    r"^(.+?)(?:\s*[xX*×]\s*|\s+)?(\d+(?:\.\d+)?)(?:\s*(?:件|包|袋|箱|瓶|听|支|条|盒|份|杯|个|桶|罐|斤|两|公斤|kg|KG|g|G|l|L|ml|ML|pcs|PCS))?$"
)
CATEGORY_HEADER_RE = re.compile(r"^(.+?)[：:]\s*$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _normalize_marker_aliases(text: str, marker: str) -> str:
    if not marker:
        return text
    pattern = re.compile(rf"{re.escape(marker)}")
    return pattern.sub(marker, text)


def _trim_edge(text: str) -> str:
    return TRIM_EDGE_RE.sub("", text or "")


def _normalize_message_text(message_text: str, start_marker: str, end_marker: str) -> str:
    text = html.unescape(message_text or "")
    text = text.replace("\ufeff", "").replace("\u200b", "")
    text = HTML_COMMENT_RE.sub("\n", text)
    text = re.sub(r"(?i)<\s*br\s*/?\s*>", "\n", text)
    text = re.sub(r"(?i)</\s*div\s*>", "\n", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _normalize_marker_aliases(text, start_marker)
    text = _normalize_marker_aliases(text, end_marker)
    text = CONTROL_RE.sub("", text)
    return text


def _extract_blocks(text: str, start_marker: str, end_marker: str) -> List[str]:
    blocks: List[str] = []
    if not text or not start_marker or not end_marker:
        return blocks

    index = 0
    while True:
        start_idx = text.find(start_marker, index)
        if start_idx < 0:
            break

        start_idx_adj = start_idx
        if start_idx > 0 and text[start_idx - 1] in {"#", "＃"}:
            start_idx_adj = start_idx - 1

        end_idx = text.find(end_marker, start_idx + len(start_marker))
        if end_idx < 0:
            break

        block = text[start_idx_adj : end_idx + len(end_marker)].strip()
        if block:
            blocks.append(block)

        index = end_idx + len(end_marker)

    return blocks


def _build_order_header_pattern(start_marker: str, digits: int = 8) -> re.Pattern[str]:
    marker = re.escape(start_marker or DEFAULT_START_MARKER)
    digit_count = max(1, int(digits))
    return re.compile(rf"^[#＃]{marker}(\d{{{digit_count}}})$")


def _is_valid_order_id_ymd_seq(order_id: str) -> bool:
    value = str(order_id or "").strip()
    if not re.fullmatch(r"\d{8}", value):
        return False
    # 规则: yymmdd + 两位序号
    try:
        datetime.strptime(value[:6], "%y%m%d")
    except ValueError:
        return False
    return True


def _extract_order_id(header_line: str, start_marker: str) -> str:
    line = (header_line or "").strip()
    if not line:
        return ""
    matched = _build_order_header_pattern(start_marker).fullmatch(line)
    if not matched:
        return ""
    order_id = matched.group(1)
    if not _is_valid_order_id_ymd_seq(order_id):
        return ""
    return order_id


def _split_block_lines(block_text: str) -> List[str]:
    return [line.strip() for line in block_text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def _normalize_payload_line(line: str) -> str:
    return " ".join(str(line or "").replace("\t", " ").split()).strip()


def _parse_count_line(line: str) -> Optional[Tuple[str, float]]:
    cleaned = _trim_edge(_normalize_payload_line(line))
    if not cleaned:
        return None

    matched = COUNT_LINE_RE.match(cleaned)
    if not matched:
        return None

    item_raw, count_raw = matched.groups()
    item = _trim_edge(" ".join(item_raw.strip().split()))
    if not item:
        return None

    try:
        count = float(count_raw)
    except ValueError:
        return None

    return item, count


def _extract_name_from_payload(payload_lines: List[str]) -> Tuple[str, List[str]]:
    if not payload_lines:
        return "", []

    first_line = _normalize_payload_line(payload_lines[0])
    if not first_line:
        return "", payload_lines[1:]

    if CATEGORY_HEADER_RE.fullmatch(first_line):
        return "", payload_lines
    if _parse_count_line(first_line) is not None:
        return "", payload_lines
    if SEPARATOR_ONLY_RE.fullmatch(_trim_edge(first_line)):
        return "", payload_lines

    # 约定：首个非数量行视为名字；未提供时保持空字符串。
    return _trim_edge(first_line), payload_lines[1:]


def _parse_payload_lines(payload_lines: List[str]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    category_prefix = ""
    for line in payload_lines:
        normalized = _normalize_payload_line(line)
        if not normalized:
            continue
        if SEPARATOR_ONLY_RE.fullmatch(_trim_edge(normalized)):
            continue

        category_match = CATEGORY_HEADER_RE.fullmatch(normalized)
        if category_match:
            category_prefix = _trim_edge(category_match.group(1))
            continue

        parsed = _parse_count_line(normalized)
        if parsed is None:
            continue
        item, count = parsed

        if category_prefix and ("：" not in item and ":" not in item):
            item = f"{category_prefix}：{item}"

        records.append({"item": item, "amount": count, "record_type": "count"})

    return records


def _parse_block(
    block_text: str,
    start_marker: str,
    end_marker: str,
) -> Tuple[List[Dict[str, Any]], str, str, str]:
    lines = _split_block_lines(block_text)
    if len(lines) < 3:
        return [], "", "", ""

    order_id = _extract_order_id(lines[0], start_marker)
    if not order_id:
        return [], "", "", ""
    if lines[-1] != end_marker:
        return [], "", "", ""

    payload_lines = lines[1:-1]
    if not payload_lines:
        return [], "", "", ""

    customer_name, payload_for_parse = _extract_name_from_payload(payload_lines)
    if not payload_for_parse:
        return [], "", "", ""

    parsed = _parse_payload_lines(payload_for_parse)
    if not parsed:
        return [], "", "", ""

    # Keep cleaned block as raw message so CSV never stores a full memory blob.
    cleaned_block = "\n".join(lines)
    return parsed, cleaned_block, order_id, customer_name


def parse_ledger_message(
    message_text: str, start_marker: str, end_marker: str = END_MARKER_DEFAULT
) -> Optional[Dict[str, Any]]:
    parsed = parse_ledger_message_multi(message_text, start_marker, end_marker=end_marker)
    if not parsed:
        return None
    return parsed[0]


def parse_ledger_message_multi(
    message_text: str, start_marker: str, end_marker: str = END_MARKER_DEFAULT
) -> List[Dict[str, Any]]:
    if not message_text or not start_marker:
        return []

    normalized = _normalize_message_text(message_text, start_marker, end_marker)
    blocks = _extract_blocks(normalized, start_marker, end_marker)
    if not blocks:
        return []

    results: List[Dict[str, Any]] = []
    for block in blocks:
        entries, cleaned_block, order_id, customer_name = _parse_block(block, start_marker, end_marker)
        if not entries or not cleaned_block or not order_id:
            continue

        for entry in entries:
            results.append(
                {
                    "item": entry["item"],
                    "amount": entry["amount"],
                    "record_type": "count",
                    "raw_message": cleaned_block,
                    "order_id": order_id,
                    "name": customer_name,
                }
            )

    return results


def parse_messages(
    messages: List[Dict[str, str]],
    start_marker: str,
    end_marker: str = END_MARKER_DEFAULT,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for msg in messages:
        parsed_list = parse_ledger_message_multi(
            msg.get("message", ""),
            start_marker,
            end_marker=end_marker,
        )
        if not parsed_list:
            continue

        timestamp = (msg.get("timestamp") or "").strip()
        source_id = (msg.get("source_id") or "").strip()
        message_hash = (msg.get("message_hash") or "").strip()
        message_captured_at = (msg.get("message_captured_at") or "").strip()
        message_name = (msg.get("name") or msg.get("customer_name") or "").strip()
        for parsed in parsed_list:
            parsed["timestamp"] = timestamp
            parsed["source_id"] = source_id
            parsed["message_hash"] = message_hash
            parsed["message_captured_at"] = message_captured_at
            if not str(parsed.get("name", "")).strip():
                parsed["name"] = message_name
            records.append(parsed)

    return records
