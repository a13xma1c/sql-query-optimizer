"""Saved connection profiles, with optional OS-keyring-backed password storage.

A :class:`ConnectionProfile` is a named :class:`~sqlanalyzer.db.ConnectionConfig` that persists
across restarts (in :class:`~sqlanalyzer.gui.settings.Settings`, alongside the single
per-engine "current connection" that already existed). Everything *except* the password is
stored there in plain JSON, same as before; the password, if the user opts to save it, goes
through the `keyring <https://pypi.org/project/keyring/>`_ package into the OS credential store
(macOS Keychain, Windows Credential Locker, a Secret Service / kwallet on Linux, ...).

Keyring backend availability varies a lot - headless CI, sandboxes, and some minimal Linux
desktops have none, in which case ``keyring.get_keyring()`` returns the library's
``backends.fail.Keyring`` sentinel and every call raises ``NoKeyringError``. Every keyring call
in this module is therefore wrapped: saving a password silently degrades to "not saved" and the
profile still works, just prompting for the password again next time, rather than ever crashing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from ..db import ConnectionConfig
from ..dialects import Engine
from .settings import Settings

SERVICE_NAME = "SQLAnalyzer"


def keyring_available() -> bool:
    """Whether saving/loading a password would actually do anything on this machine."""
    try:
        import keyring

        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001 - the keyring package itself may be missing
        return False
    return "backends.fail" not in type(backend).__module__


def set_password(profile_id: str, password: str) -> bool:
    """Best-effort save to the OS keyring; returns whether it actually ended up there."""
    if not password:
        return False
    try:
        import keyring

        keyring.set_password(SERVICE_NAME, profile_id, password)
        return True
    except Exception:  # noqa: BLE001 - NoKeyringError, PasswordSetError, a missing package, ...
        return False


def get_password(profile_id: str) -> str:
    try:
        import keyring

        return keyring.get_password(SERVICE_NAME, profile_id) or ""
    except Exception:  # noqa: BLE001
        return ""


def delete_password(profile_id: str) -> None:
    try:
        import keyring

        keyring.delete_password(SERVICE_NAME, profile_id)
    except Exception:  # noqa: BLE001 - including "there was nothing to delete"
        pass


def slug(name: str) -> str:
    """A stable, filesystem/keyring-safe id derived from a profile name."""
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "profile"
    return base


@dataclass
class ConnectionProfile:
    id: str  # stable key: used for both the settings entry and the keyring username
    name: str
    config: ConnectionConfig  # password is always blank here; see ProfileStore.resolve()
    password_saved: bool = False  # a password for this profile is believed to be in the keyring


@dataclass
class ProfileStore:
    """Named connections for one engine, persisted in ``Settings``."""

    settings: Settings
    engine: Engine
    profiles: dict[str, ConnectionProfile] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load()

    @property
    def _prefix(self) -> str:
        return f"profiles/{self.engine.value}/"

    def _load(self) -> None:
        raw = self.settings.value(self._prefix + "list", []) or []
        if isinstance(raw, str):
            raw = [raw] if raw else []
        self.profiles = {}
        for pid in raw:
            data = self.settings.value(f"{self._prefix}{pid}", {})
            if not isinstance(data, dict):
                continue
            cfg = ConnectionConfig(
                self.engine, str(data.get("host", "")), int(data["port"]) if data.get("port") else None,
                str(data.get("database", "")), str(data.get("schema", "")), str(data.get("username", "")), "",
                {str(k): str(v) for k, v in dict(data.get("extras", {})).items()})
            self.profiles[pid] = ConnectionProfile(pid, str(data.get("name", pid)), cfg,
                                                  bool(data.get("password_saved")))

    def _persist_list(self) -> None:
        self.settings.set_value(self._prefix + "list", sorted(self.profiles))

    def save(self, name: str, config: ConnectionConfig, password: str, *,
             profile_id: str | None = None, remember_password: bool = True) -> ConnectionProfile:
        """Create or update a profile. Returns it (its ``config.password`` is always blank)."""
        pid = profile_id or self._unique_id(slug(name))
        saved = remember_password and set_password(pid, password) if password else False
        if not saved and pid in self.profiles and self.profiles[pid].password_saved and not remember_password:
            delete_password(pid)
        data = {"name": name, "host": config.host, "port": config.port, "database": config.database,
                "schema": config.schema, "username": config.username, "extras": dict(config.extras),
                "password_saved": saved}
        self.settings.set_value(f"{self._prefix}{pid}", data)
        profile = ConnectionProfile(pid, name, replace(config, password=""), saved)
        self.profiles[pid] = profile
        self._persist_list()
        return profile

    def _unique_id(self, base: str) -> str:
        if base not in self.profiles:
            return base
        n = 2
        while f"{base}-{n}" in self.profiles:
            n += 1
        return f"{base}-{n}"

    def delete(self, profile_id: str) -> None:
        if profile_id not in self.profiles:
            return
        del self.profiles[profile_id]
        self.settings.remove(f"{self._prefix}{profile_id}")
        delete_password(profile_id)
        self._persist_list()

    def resolve(self, profile_id: str) -> ConnectionConfig:
        """The profile's connection config with its password filled in from the keyring."""
        profile = self.profiles[profile_id]
        password = get_password(profile_id) if profile.password_saved else ""
        return replace(profile.config, password=password)
