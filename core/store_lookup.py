"""Deterministic known-store lookup interface."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple

TraceFn = Optional[Callable[[str], None]]
_SPACE_RE = re.compile(r"\s+")
_STORE_NAME_KEYS: Tuple[str, ...] = ("name", "store", "shop", "merchant")
_STORE_ALIAS_KEYS: Tuple[str, ...] = ("aliases", "alias", "keywords", "match")


def _trace(trace: TraceFn, message: str) -> None:
    if trace is None:
        return
    try:
        trace(message)
    except Exception:
        pass


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y", "t"}:
        return True
    if text in {"0", "false", "no", "off", "n", "f"}:
        return False
    return default


def _normalize_text(value: Any) -> str:
    text = str(value or "").replace("\ufeff", "").strip().lower()
    return _SPACE_RE.sub(" ", text)


def _iter_alias_texts(raw_aliases: Any) -> Iterable[str]:
    if raw_aliases is None:
        return []
    if isinstance(raw_aliases, str):
        return [raw_aliases]
    if isinstance(raw_aliases, (list, tuple, set)):
        return [str(x) for x in raw_aliases if str(x).strip()]
    return [str(raw_aliases)]


def _parse_store_entry(entry: Any) -> Optional[Tuple[str, List[str]]]:
    if isinstance(entry, str):
        name = str(entry).strip()
        if not name:
            return None
        return name, [name]

    if not isinstance(entry, dict):
        return None

    name = ""
    for key in _STORE_NAME_KEYS:
        value = entry.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            name = text
            break
    if not name:
        return None

    aliases: List[str] = [name]
    for key in _STORE_ALIAS_KEYS:
        if key in entry:
            aliases.extend(list(_iter_alias_texts(entry.get(key))))
            break
    return name, aliases


def _iter_store_entries(payload: Any) -> Iterable[Tuple[str, List[str]]]:
    if isinstance(payload, list):
        for item in payload:
            parsed = _parse_store_entry(item)
            if parsed:
                yield parsed
        return

    if not isinstance(payload, dict):
        return

    stores_section = payload.get("stores")
    if isinstance(stores_section, list):
        for item in stores_section:
            parsed = _parse_store_entry(item)
            if parsed:
                yield parsed
        return

    for key, value in payload.items():
        if key in {"stores", "version", "updated_at", "notes", "meta"}:
            continue
        name = str(key).strip()
        if not name:
            continue
        aliases = [name]
        aliases.extend(list(_iter_alias_texts(value)))
        yield name, aliases


def build_known_store_lookup(
    config: Mapping[str, Any],
    *,
    enabled_key: str,
    path_key: str,
    default_enabled: bool,
    default_path: str,
    trace: TraceFn = None,
) -> Dict[str, Any]:
    enabled = _coerce_bool(config.get(enabled_key, default_enabled), default_enabled)
    path_text = str(config.get(path_key, default_path) or default_path).strip() or default_path
    lookup: Dict[str, Any] = {
        "enabled": enabled,
        "path": path_text,
        "alias_pairs": [],
        "stores_count": 0,
    }
    if not enabled:
        return lookup

    path = Path(path_text)
    resolved_path = path
    if not resolved_path.exists() and resolved_path.suffix == ".json" and not resolved_path.name.endswith(".example.json"):
        fallback_name = resolved_path.name
        if fallback_name.endswith(".local.json"):
            fallback_name = fallback_name[: -len(".local.json")] + ".example.json"
        else:
            fallback_name = resolved_path.stem + ".example.json"
        fallback_path = resolved_path.with_name(fallback_name)
        if fallback_path.exists():
            resolved_path = fallback_path
    if not resolved_path.exists():
        _trace(trace, f"[STORE] known store lookup enabled, but file not found: {path}")
        return lookup

    lookup["path"] = str(resolved_path)
    try:
        with resolved_path.open("r", encoding="utf-8-sig") as f:
            payload = json.load(f)
    except Exception as exc:
        _trace(trace, f"[STORE] failed to load known store file: {resolved_path}, error={exc}")
        return lookup

    alias_pairs: List[Tuple[str, str]] = []
    seen: Set[Tuple[str, str]] = set()
    store_names: Set[str] = set()
    for store_name, aliases in _iter_store_entries(payload):
        store_name_clean = str(store_name).strip()
        if not store_name_clean:
            continue
        store_names.add(store_name_clean)
        for alias in aliases:
            alias_norm = _normalize_text(alias)
            if len(alias_norm) < 2:
                continue
            pair = (alias_norm, store_name_clean)
            if pair in seen:
                continue
            seen.add(pair)
            alias_pairs.append(pair)

    alias_pairs.sort(key=lambda x: (-len(x[0]), x[0], x[1]))
    lookup["alias_pairs"] = alias_pairs
    lookup["stores_count"] = len(store_names)
    _trace(trace, f"[STORE] loaded known stores: stores={len(store_names)}, aliases={len(alias_pairs)}, path={resolved_path}")
    return lookup


def lookup_known_stores(text: str, lookup: Mapping[str, Any], max_matches: int = 3) -> List[str]:
    if not _coerce_bool(lookup.get("enabled", False), False):
        return []
    alias_pairs = lookup.get("alias_pairs")
    if not isinstance(alias_pairs, list) or not alias_pairs:
        return []

    normalized_text = _normalize_text(text)
    if not normalized_text:
        return []

    matched: List[str] = []
    seen_names: Set[str] = set()
    for alias, store_name in alias_pairs:
        if not isinstance(alias, str) or not isinstance(store_name, str):
            continue
        if alias and alias in normalized_text and store_name not in seen_names:
            seen_names.add(store_name)
            matched.append(store_name)
            if max_matches > 0 and len(matched) >= max_matches:
                break
    return matched


def annotate_records_with_known_stores(
    records: List[Dict[str, Any]],
    lookup: Mapping[str, Any],
    max_matches: int = 3,
) -> int:
    if not records:
        return 0
    if not _coerce_bool(lookup.get("enabled", False), False):
        return 0

    hit_count = 0
    for record in records:
        explicit_store_name = str(record.get("known_store", "") or record.get("store_name", "") or "")
        if explicit_store_name:
            matches = lookup_known_stores(explicit_store_name, lookup, max_matches=max_matches)
            if matches:
                hit_count += 1
                record["known_store"] = matches[0]
                record["known_store_candidates"] = matches
                continue

        raw_message = str(record.get("raw_message", "") or "")
        item = str(record.get("item", "") or "")
        matches = lookup_known_stores(f"{raw_message}\n{item}", lookup, max_matches=max_matches)
        if not matches:
            continue
        hit_count += 1
        record["known_store"] = matches[0]
        record["known_store_candidates"] = matches
    return hit_count
