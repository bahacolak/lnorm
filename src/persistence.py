"""
persistence.py - Shared helpers for writing output artifacts.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


def write_json(path: str | Path, payload: Any, *, logger: logging.Logger | None = None, message: str | None = None) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if logger and message:
        logger.info("%s: %s", message, target)
    return target
