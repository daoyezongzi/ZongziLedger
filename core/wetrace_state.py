from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict


@dataclass
class WetraceState:
    last_seq: int = 0
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "last_seq": int(self.last_seq or 0),
            "updated_at": str(self.updated_at or ""),
        }


def _to_int(value: Any, default: int = 0, minimum: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        parsed = default
    if parsed < minimum:
        return minimum
    return parsed


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_wetrace_state(path: Path) -> WetraceState:
    if not path.exists():
        return WetraceState(last_seq=0, updated_at="")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return WetraceState(last_seq=0, updated_at="")

    if not isinstance(payload, dict):
        return WetraceState(last_seq=0, updated_at="")

    return WetraceState(
        last_seq=_to_int(payload.get("last_seq"), default=0, minimum=0),
        updated_at=str(payload.get("updated_at", "") or "").strip(),
    )


def save_wetrace_state(path: Path, state: WetraceState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = state.to_dict()
    if not payload.get("updated_at"):
        payload["updated_at"] = _now_text()

    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(path)

