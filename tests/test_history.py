"""Query history store."""

import json

from sqlanalyzer.history import ENV_VAR, HistoryEntry, QueryHistory, default_path, fingerprint, normalize


def test_record_dedupes_by_normalized_sql(tmp_path):
    h = QueryHistory(tmp_path / "h.json")
    first = h.record("SELECT a FROM t", "duckdb", findings=3, complexity=12, cost=100.0, rewrites=2)
    again = h.record("select   a\nFROM t ", "duckdb", source="explain", plan_source="explain", findings=1)
    assert first is again and again.runs == 2 and again.source == "explain" and again.plan_source == "explain"
    assert again.findings == 1 and again.sql == "select   a\nFROM t "
    assert len(h) == 1 and normalize(" A  b ") == "a b" and fingerprint("SELECT 1", "duckdb") != fingerprint("SELECT 1", "trino")
    assert h.record("   ", "duckdb") is None


def test_entries_are_newest_first_filtered_and_searchable(tmp_path):
    h = QueryHistory(tmp_path / "h.json")
    h.record("SELECT 1", "duckdb")
    h.record("SELECT 2 FROM orders", "postgres", tags=["nightly"])
    h.record("SELECT 3 FROM customers", "postgres")
    assert [e.title for e in h.entries()] == ["SELECT 3 FROM customers", "SELECT 2 FROM orders", "SELECT 1"]
    assert [e.title for e in h.entries(engine="postgres")] == ["SELECT 3 FROM customers", "SELECT 2 FROM orders"]
    assert [e.title for e in h.entries(query="ORDERS")] == ["SELECT 2 FROM orders"]
    assert [e.title for e in h.entries(query="nightly")] == ["SELECT 2 FROM orders"]
    assert h.entries(query="nothing") == []


def test_persistence_limit_tags_remove_clear(tmp_path):
    path = tmp_path / "h.json"
    h = QueryHistory(path, limit=2)
    ids = [h.record(f"SELECT {i}", "duckdb").id for i in range(3)]
    assert len(h) == 2 and h.get(ids[0]) is None  # oldest trimmed
    h.tag(ids[2], ["b", "a"])
    reloaded = QueryHistory(path, limit=2)
    assert reloaded.get(ids[2]).tags == ["a", "b"] and len(reloaded) == 2
    assert reloaded.remove(ids[2]) and not reloaded.remove("missing") and len(reloaded) == 1
    reloaded.clear()
    assert len(QueryHistory(path)) == 0 and json.loads(path.read_text()) == []


def test_title_skips_comments_and_truncates():
    e = HistoryEntry("x", "duckdb", "-- comment\n\nSELECT " + "a, " * 60 + "b FROM t", "2024-01-01T00:00:00+00:00",
                     "2024-01-01T00:00:00+00:00")
    assert e.title.startswith("SELECT a, a") and e.title.endswith("…") and len(e.title) <= 91
    assert e.last_seen_local.startswith("2024-01-01") or e.last_seen_local.startswith("2023-12-31")


def test_corrupt_file_and_unknown_fields_are_tolerated(tmp_path):
    path = tmp_path / "h.json"
    path.write_text("{not json")
    assert len(QueryHistory(path)) == 0
    path.write_text(json.dumps([{"id": "a", "engine": "duckdb", "sql": "SELECT 1", "first_seen": "t", "last_seen": "t",
                                 "future_field": 1}, {"bogus": True}, 5]))
    h = QueryHistory(path)
    assert len(h) == 1 and h.get("a").sql == "SELECT 1"


def test_export_and_default_path(tmp_path, monkeypatch):
    h = QueryHistory(tmp_path / "h.json")
    h.record("SELECT 1;", "duckdb", tags=["t"])
    script = h.export_sql()
    assert "-- " in script and "SELECT 1;" in script and "t" in script and script.count("SELECT 1;") == 1
    monkeypatch.setenv(ENV_VAR, str(tmp_path / "custom.json"))
    assert default_path() == tmp_path / "custom.json"
    monkeypatch.delenv(ENV_VAR)
    assert default_path().name == "history.json"


def test_entries_recorded_within_the_same_clock_tick_stay_newest_first(tmp_path, monkeypatch):
    """Windows' clock advances in ~15 ms steps, so consecutive records can share a timestamp."""
    import sqlanalyzer.history as history_module

    class _FrozenDatetime(history_module.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2024, 1, 1, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(history_module, "datetime", _FrozenDatetime)
    h = QueryHistory(tmp_path / "h.json")
    for n in range(1, 4):
        h.record(f"SELECT {n}", "duckdb")
    assert len({e.last_seen for e in h.entries()}) == 1  # all three share one timestamp
    assert [e.title for e in h.entries()] == ["SELECT 3", "SELECT 2", "SELECT 1"]
    h.record("SELECT 1", "duckdb")  # re-running moves an entry back to the top
    assert [e.title for e in h.entries()] == ["SELECT 1", "SELECT 3", "SELECT 2"]
    # the order survives a reload, and new records keep counting from where the file left off
    reloaded = QueryHistory(tmp_path / "h.json")
    assert [e.title for e in reloaded.entries()] == ["SELECT 1", "SELECT 3", "SELECT 2"]
    reloaded.record("SELECT 4", "duckdb")
    assert [e.title for e in reloaded.entries()][:2] == ["SELECT 4", "SELECT 1"]
