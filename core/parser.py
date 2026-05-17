"""Parser for count-based ledger blocks between start/end markers."""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.constants import DEFAULT_END_MARKER, DEFAULT_START_MARKER

END_MARKER_DEFAULT = DEFAULT_END_MARKER

TRIM_EDGE_RE = re.compile(r"^[,，;；:：|/\\\-~.。\s]+|[,，;；:：|/\\\-~.。\s]+$")
SEPARATOR_ONLY_RE = re.compile(r"^[,，;；:：|/\\\-~.。\s]+$")
COUNT_UNITS_RE = r"件|包|袋|箱|瓶|听|支|条|盒|份|杯|个|桶|罐|斤|两|公斤|kg|KG|g|G|l|L|ml|ML|mL|Ml|pcs|PCS"
COUNT_LINE_RE = re.compile(
    rf"^(.+?)(?:\s*[xX*×]\s*|\s+)?(\d+(?:\.\d+)?)(?:\s*({COUNT_UNITS_RE}))?$"
)
CATEGORY_HEADER_RE = re.compile(r"^(.+?)[：:]\s*$")
HTML_TAG_RE = re.compile(r"<[^>]+>")
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LEGACY_ORDER_ID_LINE_RE = re.compile(r"^\d{8}$")
VOLUME_TOKEN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([mM]?[lL])$")
DOUBLE_VOLUME_LINE_RE = re.compile(r"^(\d{2,4})\s+(\d+(?:\.\d+)?\s*[lL])$")
VOLUME_SUFFIX_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([mM]?[lL])(?=$|[^A-Za-z0-9])")
ITEM_BARE_ML_RE = re.compile(r"(?<!\d)(500|900)(?!\d)(?!\s*[mM]?[lL])")
INLINE_ITEM_SPLIT_RE = re.compile(r"[、，,；;銆]+")
REMARK_PREFIX_SPLIT_RE = re.compile(
    r"(?:^|\s)(?:补货测试|第[一二三四五六七八九十0-9]+批次|批次|备注|带\d+月费用|旧货处理|上月|欠款)(?:\s+|$)"
)
KNOWN_STANDALONE_ML_VALUES = {
    250,
    330,
    500,
    550,
    600,
    650,
    680,
    750,
    900,
    1000,
    1250,
    1500,
    2000,
}

_PRODUCT_ALIAS_MAP: Optional[Dict[str, str]] = None
_SPEC_ALIAS_MAP: Optional[Dict[str, str]] = None
_PRODUCT_CANONICAL_SET: Optional[set[str]] = None


def _normalize_marker_aliases(text: str, marker: str) -> str:
    if not marker:
        return text
    pattern = re.compile(rf"{re.escape(marker)}")
    return pattern.sub(marker, text)


def _trim_edge(text: str) -> str:
    return TRIM_EDGE_RE.sub("", text or "")


def _resolve_dictionary_path(path: Path) -> Path:
    if path.exists():
        return path
    if path.suffix == ".json" and not path.name.endswith(".example.json"):
        name = path.name
        if name.endswith(".local.json"):
            fallback_name = name[: -len(".local.json")] + ".example.json"
        else:
            fallback_name = path.stem + ".example.json"
        fallback = path.with_name(fallback_name)
        if fallback.exists():
            return fallback
    return path


def _load_json_dict(path: Path) -> Dict[str, Any]:
    resolved = _resolve_dictionary_path(path)
    try:
        with resolved.open("r", encoding="utf-8-sig") as f:
            payload = json.load(f)
        if isinstance(payload, dict):
            return payload
    except Exception:
        return {}
    return {}


def _get_product_alias_map() -> Dict[str, str]:
    global _PRODUCT_ALIAS_MAP
    if _PRODUCT_ALIAS_MAP is not None:
        return _PRODUCT_ALIAS_MAP
    path = Path("dictionaries/products.local.json")
    payload = _load_json_dict(path)
    alias_map: Dict[str, str] = {}
    for item in payload.get("products", []) if isinstance(payload.get("products", []), list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        aliases = item.get("aliases", [])
        if not isinstance(aliases, list):
            aliases = []
        for raw_alias in [name, *aliases]:
            alias = str(raw_alias or "").strip()
            if alias:
                alias_map[alias] = name
    _PRODUCT_ALIAS_MAP = alias_map
    return _PRODUCT_ALIAS_MAP


def _get_product_canonical_set() -> set[str]:
    global _PRODUCT_CANONICAL_SET
    if _PRODUCT_CANONICAL_SET is not None:
        return _PRODUCT_CANONICAL_SET
    product_map = _get_product_alias_map()
    _PRODUCT_CANONICAL_SET = {str(v).strip() for v in product_map.values() if str(v).strip()}
    return _PRODUCT_CANONICAL_SET


def _get_spec_alias_map() -> Dict[str, str]:
    global _SPEC_ALIAS_MAP
    if _SPEC_ALIAS_MAP is not None:
        return _SPEC_ALIAS_MAP
    path = Path("dictionaries/specs.local.json")
    payload = _load_json_dict(path)
    alias_map: Dict[str, str] = {}
    entries = payload.get("spec_aliases", [])
    if isinstance(entries, list):
        for item in entries:
            if not isinstance(item, dict):
                continue
            raw = str(item.get("raw", "")).strip()
            normalized = str(item.get("normalized", "")).strip()
            if raw and normalized:
                alias_map[raw] = normalized
    _SPEC_ALIAS_MAP = alias_map
    return _SPEC_ALIAS_MAP


def _normalize_spec_token(raw_spec: str) -> str:
    spec = str(raw_spec or "").strip()
    if not spec:
        return ""
    spec_map = _get_spec_alias_map()
    if spec in spec_map:
        return spec_map[spec]
    return spec


def _canonicalize_item_with_dictionary(item_text: str) -> str:
    text = _normalize_payload_line(item_text)
    if not text:
        return ""
    product_map = _get_product_alias_map()
    if not product_map:
        return text
    # longest-first to avoid short alias swallowing long alias
    for alias in sorted(product_map.keys(), key=len, reverse=True):
        if alias and alias in text:
            canonical = product_map[alias]
            return text.replace(alias, canonical, 1)
    return text


def _is_dictionary_item(item_text: str) -> bool:
    text = _normalize_payload_line(item_text)
    if not text:
        return False
    # Allow prefixed category/spec labels like "1L：红茶", but validate the base item only.
    if "：" in text:
        text = text.split("：", 1)[1].strip()
    elif ":" in text:
        text = text.split(":", 1)[1].strip()
    if not text:
        return False
    return text in _get_product_canonical_set()


def _strip_remark_prefix_for_match(item_text: str) -> tuple[str, str]:
    """
    Split probable remark prefix from an item segment.
    Returns (clean_item_text, remark_prefix_text).
    """
    text = _normalize_payload_line(item_text)
    if not text:
        return "", ""
    original = text
    remark_parts: List[str] = []
    changed = True
    while changed and text:
        changed = False
        m = REMARK_PREFIX_SPLIT_RE.search(text)
        if m and m.start() <= 2:
            token = _trim_edge(m.group(0))
            if token:
                remark_parts.append(token)
            text = _trim_edge(text[m.end() :])
            changed = True
    if not text:
        return original, ""
    return text, " ".join([x for x in remark_parts if x]).strip()


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


def _build_order_header_pattern(
    start_marker: str,
    digits: int = 8,
    require_hash: bool = False,
) -> re.Pattern[str]:
    marker = re.escape(start_marker or DEFAULT_START_MARKER)
    digit_count = max(1, int(digits))
    hash_part = r"[#＃]" if require_hash else r"[#＃]?"
    return re.compile(rf"^{hash_part}\s*{marker}\s*(\d{{{digit_count}}})$")


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
    matched = _build_order_header_pattern(start_marker, require_hash=True).fullmatch(line)
    if not matched:
        matched = _build_order_header_pattern(start_marker, require_hash=False).fullmatch(line)
    if not matched:
        return ""
    order_id = matched.group(1)
    if not _is_valid_order_id_ymd_seq(order_id):
        return ""
    return order_id


def _split_block_lines(block_text: str) -> List[str]:
    return [line.strip() for line in block_text.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()]


def _normalize_payload_line(line: str) -> str:
    collapsed = " ".join(str(line or "").replace("\t", " ").split()).strip()
    if not collapsed:
        return ""

    def _replace_volume_suffix(matched: re.Match[str]) -> str:
        number_text, unit_text = matched.groups()
        compact_number = _compact_number_text(number_text)
        if unit_text.lower() == "ml":
            return f"{compact_number}ml"
        return f"{compact_number}L"

    return VOLUME_SUFFIX_RE.sub(_replace_volume_suffix, collapsed)


def _compact_number_text(number_text: str) -> str:
    text = str(number_text or "").strip()
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _normalize_volume_token(token: str) -> str:
    value = (
        _normalize_payload_line(token)
        .replace("Ｌ", "L")
        .replace("ｌ", "l")
        .replace("Ｍ", "M")
        .replace("ｍ", "m")
    )
    matched = VOLUME_TOKEN_RE.fullmatch(value)
    if not matched:
        return ""
    amount_text, unit_text = matched.groups()
    compact_amount = _compact_number_text(amount_text)
    if not compact_amount:
        return ""
    if unit_text.lower() == "ml":
        return f"{compact_amount}ml"
    return f"{compact_amount}L"


def _as_volume_header(line: str) -> str:
    normalized = _normalize_payload_line(line)
    if not normalized:
        return ""

    text = normalized
    if text.endswith(("：", ":")):
        text = text[:-1].strip()

    volume_token = _normalize_volume_token(text)
    if volume_token:
        return volume_token

    if re.fullmatch(r"\d{2,4}", text):
        value = int(text)
        if value in KNOWN_STANDALONE_ML_VALUES:
            return f"{value}ml"

    merged = DOUBLE_VOLUME_LINE_RE.fullmatch(text)
    if merged:
        # OCR occasionally merges "500" + "1l" onto one line; keep the explicit unit token.
        return _normalize_volume_token(merged.group(2))

    return ""


def _parse_count_line(line: str) -> Optional[Tuple[str, float]]:
    cleaned = _trim_edge(_normalize_payload_line(line))
    if not cleaned:
        return None

    matched = COUNT_LINE_RE.match(cleaned)
    if not matched:
        return None

    item_raw, count_raw, unit_raw = matched.groups()
    item = _trim_edge(" ".join(item_raw.strip().split()))
    if not item:
        return None
    if item.isdigit():
        # 保护：避免把纯数字单号/噪声行解析成商品名（例如 26050101 -> item=2）。
        return None

    try:
        count = float(count_raw)
    except ValueError:
        return None

    # 保护：避免把 "红茶500 1l" 这类容量规格误识别为“数量=1”。
    # 这类行通常是规格提示而不是可记账数量行。
    if str(unit_raw or "").lower() in {"l", "ml"} and count == 1.0 and re.search(r"\d$", item):
        return None

    # 先剥离备注前缀，再进入规格与词典匹配流程。
    item, _remark_prefix = _strip_remark_prefix_for_match(item)
    if not item:
        return None

    # 规格归一：把裸 500/900（常见容量规格）补成 ml，再按规格字典归一。
    item = ITEM_BARE_ML_RE.sub(lambda m: f"{m.group(1)}ml", item)
    for raw_spec, normalized_spec in _get_spec_alias_map().items():
        if raw_spec and normalized_spec:
            item = re.sub(rf"(?<!\w){re.escape(raw_spec)}(?!\w)", normalized_spec, item)

    # 商品名按字典强绑定（别名 -> 标准名）
    item = _canonicalize_item_with_dictionary(item)
    # 强门槛：仅词典命中的标准品名允许入账。
    if not _is_dictionary_item(item):
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
    if _as_volume_header(first_line):
        # 首行是规格头（例如 1L / 500），不应被当作店名。
        return "", payload_lines
    if _parse_count_line(first_line) is not None:
        return "", payload_lines
    if SEPARATOR_ONLY_RE.fullmatch(_trim_edge(first_line)):
        return "", payload_lines
    marker_like = first_line.lstrip("#＃").strip()
    if re.fullmatch(r"记账\s*\d{0,8}", marker_like):
        # 兼容偶发重复头行：不要把“记账”误识别为店名。
        return "", payload_lines[1:]

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

        volume_header = _as_volume_header(normalized)
        if volume_header:
            category_prefix = volume_header
            continue

        category_match = CATEGORY_HEADER_RE.fullmatch(normalized)
        if category_match:
            category_prefix = _trim_edge(category_match.group(1))
            continue

        segments = [normalized]
        if INLINE_ITEM_SPLIT_RE.search(normalized):
            split_parts = [_trim_edge(part) for part in INLINE_ITEM_SPLIT_RE.split(normalized)]
            segments = [part for part in split_parts if part]

        for segment in segments:
            parsed = _parse_count_line(segment)
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


def _parse_plain_message_block(message_text: str, start_marker: str) -> Tuple[List[Dict[str, Any]], str, str, str]:
    lines = _split_block_lines(message_text)
    if len(lines) < 2:
        return [], "", "", ""

    order_id = _extract_order_id(lines[0], start_marker)
    payload_lines = lines[1:] if order_id else lines
    if not order_id and payload_lines:
        first_line = _normalize_payload_line(payload_lines[0])
        if LEGACY_ORDER_ID_LINE_RE.fullmatch(first_line) and _is_valid_order_id_ymd_seq(first_line):
            # 无头模式下兼容历史“yymmdd+序号”首行：跳过该行，仅解析真实账单明细。
            payload_lines = payload_lines[1:]

    customer_name, payload_for_parse = _extract_name_from_payload(payload_lines)
    if not payload_for_parse:
        return [], "", "", ""

    parsed = _parse_payload_lines(payload_for_parse)
    if not parsed:
        return [], "", "", ""

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
    if not message_text:
        return []

    normalized = _normalize_message_text(message_text, start_marker, end_marker)
    blocks = _extract_blocks(normalized, start_marker, end_marker)
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

    if results:
        return results

    # Fallback: allow plain-text ledger without start/end markers or order id.
    entries, cleaned_block, order_id, customer_name = _parse_plain_message_block(
        normalized,
        start_marker,
    )
    if not entries or not cleaned_block:
        return []
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

        metadata: Dict[str, Any] = {}
        for key, value in msg.items():
            if key == "message":
                continue
            if isinstance(value, str):
                metadata[key] = value.strip()
            else:
                metadata[key] = value

        timestamp = str(metadata.get("timestamp") or "").strip()
        source_id = str(metadata.get("source_id") or "").strip()
        message_hash = str(metadata.get("message_hash") or "").strip()
        message_captured_at = str(metadata.get("message_captured_at") or "").strip()
        source_type = str(metadata.get("source_type") or "").strip()
        source_ref = str(metadata.get("source_ref") or "").strip()
        ocr_provider = str(metadata.get("ocr_provider") or "").strip()
        message_name = str(
            metadata.get("name")
            or metadata.get("customer_name")
            or metadata.get("sender")
            or ""
        ).strip()
        for parsed in parsed_list:
            merged = dict(parsed)
            for key, value in metadata.items():
                existing = merged.get(key)
                if existing is None or (isinstance(existing, str) and not existing.strip()):
                    merged[key] = value

            merged["timestamp"] = timestamp
            merged["source_id"] = source_id
            merged["message_hash"] = message_hash
            merged["message_captured_at"] = message_captured_at
            merged["source_type"] = source_type
            merged["source_ref"] = source_ref
            merged["ocr_provider"] = ocr_provider
            if not str(merged.get("name", "")).strip():
                merged["name"] = message_name
            if not str(merged.get("sender", "")).strip() and message_name:
                merged["sender"] = message_name
            records.append(merged)

    return records
