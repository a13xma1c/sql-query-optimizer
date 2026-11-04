"""The JSON settings store that replaces QSettings."""

import json

from sqlanalyzer.gui.settings import ENV_VAR, Settings, default_path


def test_round_trip_and_atomic_file(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    s = Settings(path)
    assert not s.contains("a") and s.value("a", 1) == 1
    s.set_value("a", True)
    s.set_value("llm/model", "m")
    s.set_value("optimizer/disabled_rules", ["x", "y"])
    s.set_value("connection/duckdb/extras", {"k": "v"})
    assert path.exists() and json.loads(path.read_text())["a"] is True
    again = Settings(path)
    assert again.value("a") is True and again.value("llm/model") == "m"
    assert again.value("optimizer/disabled_rules") == ["x", "y"]
    assert again.value("connection/duckdb/extras") == {"k": "v"}
    assert again.keys("llm/") == ["llm/model"]
    again.remove("a")
    assert not Settings(path).contains("a")
    assert not list(path.parent.glob(".settings-*"))  # no temp files left behind


def test_unchanged_values_do_not_rewrite_the_file(tmp_path):
    path = tmp_path / "settings.json"
    s = Settings(path)
    s.set_value("a", 1)
    stamp = path.stat().st_mtime_ns
    s.set_value("a", 1)
    assert path.stat().st_mtime_ns == stamp


def test_corrupt_or_missing_file_is_tolerated(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert Settings(path).value("a", "d") == "d"
    path.write_text("[1, 2]")
    assert Settings(path).keys() == []
    assert Settings(tmp_path / "missing.json").keys() == []


def test_unwritable_location_is_ignored(tmp_path):
    s = Settings(tmp_path / "file-not-dir" / "x.json")
    (tmp_path / "file-not-dir").write_text("I am a file, not a directory")
    s.set_value("a", 1)  # must not raise
    assert s.value("a") == 1


def test_default_path_honours_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "custom.json"))
    assert default_path() == tmp_path / "custom.json"
    monkeypatch.delenv(ENV_VAR)
    assert default_path().name == "settings.json"


def test_session_persists_through_the_store(tmp_path, monkeypatch):
    from sqlanalyzer.db import ConnectionConfig
    from sqlanalyzer.dialects import Engine
    from sqlanalyzer.gui.session import Session
    from sqlanalyzer.llm import LlmConfig

    path = tmp_path / "settings.json"
    session = Session(Settings(path))
    session.save_hints("orders: dt")
    session.save_schema("orders: id int")
    session.save_optimizer(False, {"union-all"})
    session.save_llm(LlmConfig(model="m", api_key="secret"))
    session.save_connection(ConnectionConfig(Engine.POSTGRES, host="h", port=5433, database="d", username="u",
                                             password="pw", extras={"sslmode": "require"}))
    text = path.read_text()
    assert "secret" not in text and "pw" not in text
    again = Session(Settings(path))
    assert again.hints_text == "orders: dt" and again.schema_text == "orders: id int"
    assert not again.include_review and again.disabled_rules == {"union-all"}
    assert again.llm.model == "m" and again.llm.api_key == ""
    pg = again.connections[Engine.POSTGRES]
    assert (pg.host, pg.port, pg.database, pg.username, pg.password, pg.extras) == ("h", 5433, "d", "u", "",
                                                                                     {"sslmode": "require"})

