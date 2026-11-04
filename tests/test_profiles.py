"""Connection profiles and keyring-backed password storage.

Most of this sandbox has no usable keyring backend (``keyring.backends.fail.Keyring``), which is
exactly the "gracefully degrade" path the module must handle - those tests run unconditionally.
A couple of tests exercise the "keyring available" path with a fake backend, monkeypatched in
through ``keyring.get_keyring`` rather than by registering a real ``KeyringBackend`` subclass:
subclasses of ``KeyringBackend`` are picked up by keyring's *global, process-wide* auto-detection
the moment the class is defined (``KeyringBackend.__subclasses__()``), which would leak a fake
backend into every other test in the suite that looks at the ambient default - not just the ones
using the fixture below.
"""

from __future__ import annotations

import keyring
import keyring.core
import pytest
from keyring.errors import NoKeyringError

from sqlanalyzer.db import ConnectionConfig
from sqlanalyzer.dialects import Engine
from sqlanalyzer.gui.profiles import (
    ProfileStore,
    delete_password,
    get_password,
    keyring_available,
    set_password,
    slug,
)
from sqlanalyzer.gui.settings import Settings


class _FakeBackend:
    """Stands in for a real OS keyring backend without touching keyring's global registry."""

    __module__ = "tests.fake_backend"  # anything other than "keyring.backends.fail"

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def set_password(self, service, username, password) -> None:
        self._store[(service, username)] = password

    def get_password(self, service, username):
        return self._store.get((service, username))

    def delete_password(self, service, username) -> None:
        if (service, username) not in self._store:
            raise NoKeyringError("not found")
        del self._store[(service, username)]


class _BrokenBackend:
    """A backend that is "found" but never actually works (e.g. a locked/misconfigured wallet)."""

    def set_password(self, service, username, password) -> None:
        raise NoKeyringError("no backend")

    def get_password(self, service, username):
        raise NoKeyringError("no backend")

    def delete_password(self, service, username) -> None:
        raise NoKeyringError("no backend")


def _patch_get_keyring(monkeypatch, backend) -> None:
    # keyring.set_password()/get_password()/delete_password() call core.get_keyring() directly
    # (not the keyring.get_keyring re-export), so both names need patching.
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring.core, "get_keyring", lambda: backend)


@pytest.fixture
def memory_keyring(monkeypatch):
    backend = _FakeBackend()
    _patch_get_keyring(monkeypatch, backend)
    return backend


@pytest.fixture
def broken_keyring(monkeypatch):
    _patch_get_keyring(monkeypatch, _BrokenBackend())


@pytest.fixture
def settings(tmp_path):
    return Settings(tmp_path / "settings.json")


# --------------------------------------------------------------- keyring helpers
def test_keyring_available_reflects_the_ambient_backend():
    # Whether a backend exists depends on the host (macOS has the Keychain, a headless Linux box usually
    # has nothing), so only check that the answer is a bool consistent with what keyring itself reports.
    backend = keyring.get_keyring()
    expected = "backends.fail" not in type(backend).__module__
    assert keyring_available() is expected


def test_keyring_available_is_false_for_the_fail_backend(monkeypatch):
    from keyring.backends import fail

    _patch_get_keyring(monkeypatch, fail.Keyring())
    assert keyring_available() is False


def test_keyring_available_is_false_when_the_package_misbehaves(monkeypatch):
    def boom():
        raise RuntimeError("no keyring for you")

    monkeypatch.setattr(keyring, "get_keyring", boom)
    assert keyring_available() is False


def test_set_and_get_password_degrade_gracefully_without_a_backend(broken_keyring):
    assert set_password("profile-1", "hunter2") is False
    assert get_password("profile-1") == ""
    delete_password("profile-1")  # must not raise


def test_set_and_get_password_round_trip_with_a_working_backend(memory_keyring):
    assert keyring_available() is True
    assert set_password("profile-1", "hunter2") is True
    assert get_password("profile-1") == "hunter2"
    delete_password("profile-1")
    assert get_password("profile-1") == ""


def test_set_password_with_empty_string_is_a_no_op(memory_keyring):
    assert set_password("profile-1", "") is False
    assert get_password("profile-1") == ""


def test_slug_produces_stable_filesystem_and_keyring_safe_ids():
    assert slug("Prod Postgres!") == "prod-postgres"
    assert slug("  spaced  out  ") == "spaced-out"
    assert slug("") == "profile"


# ------------------------------------------------------------------- ProfileStore
def test_profile_store_save_and_resolve_without_a_keyring(settings, broken_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    cfg = ConnectionConfig(Engine.POSTGRES, host="db.example.com", port=5432, database="analytics",
                           username="alice")
    profile = store.save("Prod", cfg, "s3cret")
    assert profile.id == "prod" and profile.password_saved is False
    resolved = store.resolve("prod")
    assert resolved.host == "db.example.com" and resolved.password == ""  # never saved, never returned


def test_profile_store_save_and_resolve_with_a_keyring(settings, memory_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    cfg = ConnectionConfig(Engine.POSTGRES, host="db.example.com", port=5432, database="analytics")
    profile = store.save("Prod", cfg, "s3cret")
    assert profile.password_saved is True
    assert store.resolve(profile.id).password == "s3cret"


def test_profile_store_persists_across_instances(settings, memory_keyring):
    cfg = ConnectionConfig(Engine.POSTGRES, host="h", port=5432, database="d", username="u")
    ProfileStore(settings, Engine.POSTGRES).save("Prod", cfg, "pw")
    reloaded = ProfileStore(settings, Engine.POSTGRES)
    assert "prod" in reloaded.profiles
    assert reloaded.resolve("prod").password == "pw"
    assert reloaded.resolve("prod").host == "h"


def test_profile_store_is_scoped_per_engine(settings, memory_keyring):
    ProfileStore(settings, Engine.POSTGRES).save("Prod", ConnectionConfig(Engine.POSTGRES), "pw")
    duckdb_store = ProfileStore(settings, Engine.DUCKDB)
    assert duckdb_store.profiles == {}


def test_profile_store_deduplicates_ids_from_similar_names(settings, broken_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    store.save("Prod", ConnectionConfig(Engine.POSTGRES), "")
    second = store.save("Prod", ConnectionConfig(Engine.POSTGRES, host="other"), "")
    assert second.id == "prod-2"
    assert set(store.profiles) == {"prod", "prod-2"}


def test_profile_store_delete_removes_settings_and_keyring_entry(settings, memory_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    profile = store.save("Prod", ConnectionConfig(Engine.POSTGRES), "pw")
    store.delete(profile.id)
    assert profile.id not in store.profiles
    assert get_password(profile.id) == ""
    reloaded = ProfileStore(settings, Engine.POSTGRES)
    assert profile.id not in reloaded.profiles


def test_profile_store_delete_of_unknown_id_is_a_no_op(settings, broken_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    store.delete("does-not-exist")  # must not raise


def test_saving_without_remembering_drops_a_previously_saved_password(settings, memory_keyring):
    store = ProfileStore(settings, Engine.POSTGRES)
    cfg = ConnectionConfig(Engine.POSTGRES)
    profile = store.save("Prod", cfg, "pw")
    assert profile.password_saved is True
    updated = store.save("Prod", cfg, "", profile_id=profile.id, remember_password=False)
    assert updated.password_saved is False
    assert get_password(profile.id) == ""

