"""Persistent application settings (a JSON file), replacing ``QSettings``.

A plain JSON file is easy to inspect, independent of the Qt binding's QVariant
conversions (``QSettings.setValue`` was the first place where PySide6 6.12's
borrowed-reference bug showed up, see ``sqlanalyzer.check_runtime``), and lets
tests point the store at a temporary location through ``SQLANALYZER_SETTINGS_FILE``.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ENV_VAR = "SQLANALYZER_SETTINGS_FILE"


def default_path() -> Path:
    """``$SQLANALYZER_SETTINGS_FILE`` or the platform's per-user config directory."""
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    try:
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppConfigLocation)
    except ImportError:  # pragma: no cover - headless use
        base = ""
    if not base:
        base = str(Path.home() / ".config" / "SQLAnalyzer")
    return Path(base) / "settings.json"


class Settings:
    """Flat ``key -> JSON value`` store with ``QSettings``-like ``/``-separated keys."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self._data: dict[str, Any] = {}
        self.load()

    # ----------------------------------------------------------------- access
    def value(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def contains(self, key: str) -> bool:
        return key in self._data

    def set_value(self, key: str, value: Any) -> None:
        if self._data.get(key, _MISSING) == value:
            return
        self._data[key] = value
        self.save()

    def remove(self, key: str) -> None:
        if key in self._data:
            del self._data[key]
            self.save()

    def keys(self, prefix: str = "") -> list[str]:
        return sorted(k for k in self._data if k.startswith(prefix))

    # ------------------------------------------------------------------- file
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        self._data = raw if isinstance(raw, dict) else {}

    def save(self) -> None:
        """Atomic write; failures (read-only home, ...) are ignored - settings are a convenience."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except OSError:
            pass


_MISSING = object()
