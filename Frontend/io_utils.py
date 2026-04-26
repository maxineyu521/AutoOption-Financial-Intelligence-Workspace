from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import PROJECT_ROOT


def safe_read_text(path: Path, fallback: str = "") -> str:
    try:
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception:
        return fallback
    return fallback


def safe_read_json(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def find_latest_file(pattern: str) -> Optional[Path]:
    files = sorted(PROJECT_ROOT.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def discover_final_state_files(limit: int = 40) -> List[Path]:
    files = sorted(
        PROJECT_ROOT.glob("logs/router_e2e/**/*_final_state.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return files[:limit]
