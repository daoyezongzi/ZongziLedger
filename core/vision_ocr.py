from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Optional

from core.constants import DEFAULT_VISION_OCR_PROVIDER

TraceFn = Optional[Callable[[str], None]]


def _trace(trace: TraceFn, message: str) -> None:
    if trace:
        trace(message)


def _resolve_provider(config: Dict[str, Any]) -> str:
    raw_provider = config.get("vision_ocr_provider", config.get("ocr_provider", DEFAULT_VISION_OCR_PROVIDER))
    provider = str(raw_provider or "").strip().lower()
    return provider or DEFAULT_VISION_OCR_PROVIDER


def extract_text_from_image(
    image_path: Path,
    config: Dict[str, Any],
    trace: Optional[Callable[[str], None]],
) -> str:
    provider = _resolve_provider(config)
    image_name = image_path.name

    if provider == "noop":
        _trace(trace, f"[VISION_OCR] provider={provider}, image={image_name}, skipped")
        return ""

    if provider != "tesseract":
        raise ValueError(f"OCR provider={provider} is not supported for image={image_name}")

    if not image_path.exists() or not image_path.is_file():
        raise FileNotFoundError(f"OCR provider={provider} image={image_name} not found: {image_path}")

    try:
        from PIL import Image
        import pytesseract
    except Exception as exc:
        raise RuntimeError(
            f"OCR provider={provider} image={image_name} missing dependency: {exc}"
        ) from exc

    lang = str(config.get("vision_tesseract_lang", config.get("tesseract_lang", "chi_sim+eng"))).strip()
    tesseract_cfg = str(config.get("vision_tesseract_config", config.get("tesseract_config", ""))).strip()
    kwargs: Dict[str, Any] = {}
    if lang:
        kwargs["lang"] = lang
    if tesseract_cfg:
        kwargs["config"] = tesseract_cfg

    _trace(trace, f"[VISION_OCR] provider={provider}, image={image_name}, started")
    try:
        with Image.open(image_path) as image:
            text = pytesseract.image_to_string(image, **kwargs)
    except Exception as exc:
        raise RuntimeError(f"OCR provider={provider} image={image_name} failed: {exc}") from exc

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    _trace(trace, f"[VISION_OCR] provider={provider}, image={image_name}, chars={len(normalized)}")
    return normalized
