"""Choices made on the dashboard that last across runs (e.g. the speaking voice), in
~/.config/agent-eyes/settings.json (AGENT_EYES_SETTINGS sets another file)."""

from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    return Path(os.environ.get("AGENT_EYES_SETTINGS") or Path.home() / ".config" / "agent-eyes" / "settings.json")


def load() -> dict:
    try:
        settings = json.loads(path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save(**changes) -> None:
    """Updates the given settings, keeping the others."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({**load(), **changes}, indent=2) + "\n")
