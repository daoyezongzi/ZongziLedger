"""Excel output helpers for canonical bill payloads."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List


def _sanitize_text(value: Any) -> str:
    text = str(value or "").strip()
    # Excel interprets leading formula characters in text cells. Prefixing
    # with an apostrophe keeps untrusted review/business strings literal.
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _normalize_items(raw_items: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(raw_items, list):
        return items
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        item_name = _sanitize_text(raw.get("item", ""))
        if not item_name:
            continue
        items.append(
            {
                "item": item_name,
                "amount": _to_float(raw.get("amount", 0.0)),
                "remark": _sanitize_text(raw.get("remark", "")),
            }
        )
    return items


def _safe_datetime_text(value: Any) -> str:
    raw = _sanitize_text(value)
    if not raw:
        return ""
    normalized = raw.replace("T", " ").replace("Z", "")
    if "+" in normalized:
        normalized = normalized.split("+", 1)[0].strip()
    return normalized


def _bill_event_time_text(bill: Dict[str, Any]) -> str:
    for key in ("timestamp", "message_captured_at", "recorded_at"):
        candidate = _safe_datetime_text(bill.get(key, ""))
        if candidate:
            return candidate
    return ""


def _bill_date_text(bill: Dict[str, Any]) -> str:
    event_text = _bill_event_time_text(bill)
    if len(event_text) >= 10:
        return event_text[:10]
    return ""


def write_bill_excel_output(
    excel_path: Path,
    bills: List[Dict[str, Any]],
    review_rows: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Write business-facing xlsx with business_view + review sheets only."""
    from openpyxl import Workbook

    excel_path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet_business = workbook.active
    sheet_business.title = "business_view"
    sheet_review = workbook.create_sheet("review")

    normalized_bills: List[Dict[str, Any]] = []
    for raw_bill in bills:
        if not isinstance(raw_bill, dict):
            continue
        bill = dict(raw_bill)
        bill_items = _normalize_items(bill.get("items", []))
        bill["items"] = bill_items
        bill["item_count"] = len(bill_items)
        bill["total_amount"] = sum(_to_float(x.get("amount", 0.0)) for x in bill_items)
        normalized_bills.append(bill)

    business_headers = ["store_name", "item", "amount", "remark"]
    sheet_business.append(business_headers)
    review_headers = ["timestamp", "source_id", "message_hash", "store_name", "name", "reason", "message"]
    sheet_review.append(review_headers)

    total_item_rows = 0
    total_amount = 0.0
    sorted_bills = sorted(normalized_bills, key=lambda x: _bill_event_time_text(x))
    for idx, bill in enumerate(sorted_bills, start=1):
        bill_no = f"B{idx:04d}"
        bill_time = _bill_event_time_text(bill)
        bill_date = _bill_date_text(bill)
        # Business sheet must use deterministic matched store only.
        store_name = _sanitize_text(bill.get("known_store", "")) or _sanitize_text(bill.get("store_name", ""))
        item_count = int(bill.get("item_count", 0))
        bill_total = _to_float(bill.get("total_amount", 0.0))
        total_amount += bill_total
        for item_index, item in enumerate(bill.get("items", []), start=1):
            total_item_rows += 1
            sheet_business.append(
                [
                    store_name,
                    _sanitize_text(item.get("item", "")),
                    _to_float(item.get("amount", 0.0)),
                    _sanitize_text(item.get("remark", "")),
                ]
            )

    # Merge consecutive same-store cells in column A for better readability.
    business_start_row = 2
    business_end_row = sheet_business.max_row
    if business_end_row >= business_start_row:
        merge_start = business_start_row
        prev_store = _sanitize_text(sheet_business.cell(row=business_start_row, column=1).value)
        for row in range(business_start_row + 1, business_end_row + 1):
            cur_store = _sanitize_text(sheet_business.cell(row=row, column=1).value)
            if cur_store != prev_store:
                if prev_store and row - 1 > merge_start:
                    sheet_business.merge_cells(start_row=merge_start, start_column=1, end_row=row - 1, end_column=1)
                merge_start = row
                prev_store = cur_store
        if prev_store and business_end_row > merge_start:
            sheet_business.merge_cells(
                start_row=merge_start,
                start_column=1,
                end_row=business_end_row,
                end_column=1,
            )

    # Keep worksheet focused on business-facing rows only.
    for row in (review_rows or []):
        if not isinstance(row, dict):
            continue
        sheet_review.append(
            [
                _sanitize_text(row.get("timestamp", "")),
                _sanitize_text(row.get("source_id", "")),
                _sanitize_text(row.get("message_hash", "")),
                _sanitize_text(row.get("store_name", "")),
                _sanitize_text(row.get("name", "")),
                _sanitize_text(row.get("reason", "")),
                _sanitize_text(row.get("message", "")),
            ]
        )

    sheet_business.freeze_panes = "A2"
    sheet_review.freeze_panes = "A2"

    workbook.save(excel_path)
    return {
        "bill_count": len(normalized_bills),
        "item_count": total_item_rows,
        "total_amount": total_amount,
        "review_count": len(review_rows or []),
        "path": str(excel_path),
    }
