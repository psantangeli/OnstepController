"""Tiny JSON persistence for user-adjustable UI settings (e.g. brightness).

Kept separate from config.yaml: config is the committed defaults, this is the
small set of values the user changes on the device itself and expects to stick
across restarts. Best-effort -- a missing or corrupt file just yields defaults.
"""

from __future__ import annotations

import json
import logging
import os

from .config import SETTINGS_PATH

log = logging.getLogger(__name__)


def load_settings(path: str | None = None) -> dict:
    """Return the persisted settings dict (empty if none/unreadable)."""
    path = path or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict, path: str | None = None) -> None:
    """Persist the settings dict. Best-effort; logs on failure but never raises."""
    path = path or SETTINGS_PATH
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except OSError as exc:  # pragma: no cover - non-fatal
        log.debug("could not write settings %s: %s", path, exc)
