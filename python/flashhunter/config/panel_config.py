"""Persistent configuration managed through the settings panel."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any


def settings_file_path() -> Path:
    configured = os.environ.get("FLASHHUNTER_SETTINGS_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".flashhunter" / "settings.json"


def load_panel_settings() -> dict[str, Any]:
    path = settings_file_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict) or not isinstance(payload.get("values", {}), dict):
        raise ValueError(f"invalid settings file: {path}")
    return payload.get("values", {})


def save_panel_settings(values: dict[str, Any]) -> None:
    path = settings_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    merged = load_panel_settings()
    merged.update(values)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"values": merged}, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        Path(temporary).unlink(missing_ok=True)
        raise
