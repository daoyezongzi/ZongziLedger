from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from core.constants import (
    DEFAULT_VISION_ENABLED,
    DEFAULT_VISION_IMAGE_EXTENSIONS,
    DEFAULT_VISION_INBOX_DIR,
    DEFAULT_VISION_MAX_IMAGES_PER_RUN,
    DEFAULT_VISION_OCR_PROVIDER,
    DEFAULT_VISION_STATE_PATH,
    resolve_end_marker,
    resolve_start_marker,
)
from core.parser import parse_ledger_message_multi
from core.vision_ocr import extract_text_from_image

TraceFn = Optional[Callable[[str], None]]
CONTROL_TEXT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _trace(trace: TraceFn, message: str) -> None:
    if trace:
        trace(message)


def _sanitize_text(value: Any) -> str:
    text = str(value or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = CONTROL_TEXT_RE.sub("", text)
    return text.strip()


def _resolve_provider(config: Dict[str, Any]) -> str:
    raw_provider = config.get("vision_ocr_provider", config.get("ocr_provider", DEFAULT_VISION_OCR_PROVIDER))
    provider = str(raw_provider or "").strip().lower()
    return provider or DEFAULT_VISION_OCR_PROVIDER


def _to_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "是"}:
        return True
    if text in {"0", "false", "no", "n", "off", "否"}:
        return False
    return default


def _to_int(value: Any, default: int, minimum: int = 0) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    return parsed


def _resolve_extensions(config_value: Any) -> Set[str]:
    if isinstance(config_value, str):
        raw_items = re.split(r"[,;\s]+", config_value.strip())
    elif isinstance(config_value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in config_value]
    else:
        raw_items = []

    normalized: Set[str] = set()
    for item in raw_items:
        if not item:
            continue
        ext = item.lower()
        if not ext.startswith("."):
            ext = f".{ext}"
        normalized.add(ext)

    default_exts = {str(x).strip().lower() for x in DEFAULT_VISION_IMAGE_EXTENSIONS if str(x).strip()}
    return normalized or default_exts


def _iter_image_files(inbox_dir: Path, extensions: Set[str]) -> List[Path]:
    if not inbox_dir.exists() or not inbox_dir.is_dir():
        return []
    files = [path for path in inbox_dir.rglob("*") if path.is_file() and path.suffix.lower() in extensions]
    files.sort(key=lambda path: str(path).lower())
    return files


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _load_state(path: Path) -> Set[str]:
    if not path.exists():
        return set()

    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return set()

    raw_items: Any
    if isinstance(payload, dict):
        raw_items = payload.get("seen_image_fingerprints", [])
    else:
        raw_items = payload

    if not isinstance(raw_items, list):
        return set()

    seen: Set[str] = set()
    for item in raw_items:
        digest = str(item or "").strip().lower()
        if digest:
            seen.add(digest)
    return seen


def _save_state(path: Path, seen_fingerprints: Set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "seen_image_fingerprints": sorted(seen_fingerprints),
    }
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _excerpt(text: Any, limit: int = 200) -> str:
    normalized = _sanitize_text(text)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit]


def _build_failure(
    reason: str,
    image_path: Path,
    error: Any = "",
    raw_text_excerpt: Any = "",
) -> Dict[str, str]:
    return {
        "reason": _sanitize_text(reason),
        "image_path": str(image_path),
        "error": _sanitize_text(error),
        "raw_text_excerpt": _excerpt(raw_text_excerpt),
    }


def collect_messages_from_images(
    config: Dict[str, Any],
    trace: TraceFn = None,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], Dict[str, int]]:
    if not _to_bool(config.get("vision_enabled", DEFAULT_VISION_ENABLED), DEFAULT_VISION_ENABLED):
        _trace(trace, "[VISION_INGEST] vision_enabled=false, skip image ingest")
        return [], [], {"scanned_images": 0, "accepted_messages": 0, "failed_images": 0, "dedup_skipped": 0}

    inbox_raw = str(config.get("vision_inbox_dir", DEFAULT_VISION_INBOX_DIR)).strip() or DEFAULT_VISION_INBOX_DIR
    inbox_dir = Path(inbox_raw)
    state_path = Path(
        str(config.get("vision_state_path", DEFAULT_VISION_STATE_PATH)).strip() or DEFAULT_VISION_STATE_PATH
    )
    extensions = _resolve_extensions(config.get("vision_image_extensions", DEFAULT_VISION_IMAGE_EXTENSIONS))
    max_images = _to_int(
        config.get("vision_max_images_per_run", DEFAULT_VISION_MAX_IMAGES_PER_RUN),
        DEFAULT_VISION_MAX_IMAGES_PER_RUN,
        minimum=0,
    )
    provider = _resolve_provider(config)
    source_prefix = _sanitize_text(config.get("vision_source_id", "vision")) or "vision"
    start_marker = resolve_start_marker(config)
    end_marker = resolve_end_marker(config)

    messages: List[Dict[str, str]] = []
    failures: List[Dict[str, str]] = []
    stats: Dict[str, int] = {
        "scanned_images": 0,
        "accepted_messages": 0,
        "failed_images": 0,
        "dedup_skipped": 0,
    }

    image_paths = _iter_image_files(inbox_dir, extensions)
    if max_images > 0:
        image_paths = image_paths[:max_images]
    stats["scanned_images"] = len(image_paths)
    if not image_paths:
        _trace(trace, f"[VISION_INGEST] no image found in {inbox_dir}")
        return messages, failures, stats

    seen_fingerprints = _load_state(state_path)
    _trace(
        trace,
        f"[VISION_INGEST] inbox={inbox_dir}, scanned={len(image_paths)}, state_size={len(seen_fingerprints)}, provider={provider}",
    )

    for image_path in image_paths:
        try:
            fingerprint = _sha1_file(image_path)
        except Exception as exc:
            failures.append(_build_failure("fingerprint_error", image_path, exc, ""))
            stats["failed_images"] += 1
            continue

        if fingerprint in seen_fingerprints:
            stats["dedup_skipped"] += 1
            continue

        try:
            raw_text = extract_text_from_image(image_path, config, trace)
        except Exception as exc:
            failures.append(_build_failure("ocr_error", image_path, exc, ""))
            stats["failed_images"] += 1
            continue

        message_text = _sanitize_text(raw_text)
        if not message_text:
            failures.append(
                _build_failure(
                    "empty_message",
                    image_path,
                    "OCR text is empty after normalization",
                    raw_text,
                )
            )
            stats["failed_images"] += 1
            continue

        if not parse_ledger_message_multi(message_text, start_marker, end_marker=end_marker):
            failures.append(
                _build_failure(
                    "invalid_ledger_block",
                    image_path,
                    f"message is not parseable as '#{start_marker}...{end_marker}' block",
                    message_text,
                )
            )
            stats["failed_images"] += 1
            continue

        captured_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            timestamp = datetime.fromtimestamp(image_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            timestamp = captured_at

        messages.append(
            {
                "message": message_text,
                "timestamp": timestamp,
                "source_id": f"{source_prefix}:{fingerprint[:12]}",
                "message_hash": _sha1_text(message_text),
                "message_captured_at": captured_at,
                "source_type": "image",
                "source_ref": str(image_path.resolve()),
                "ocr_provider": provider,
            }
        )
        seen_fingerprints.add(fingerprint)
        stats["accepted_messages"] += 1

    _save_state(state_path, seen_fingerprints)
    _trace(
        trace,
        f"[VISION_INGEST] done: scanned={stats['scanned_images']}, accepted={stats['accepted_messages']}, failed={stats['failed_images']}, dedup_skipped={stats['dedup_skipped']}",
    )
    return messages, failures, stats
