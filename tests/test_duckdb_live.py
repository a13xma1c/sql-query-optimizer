import duckdb
import pytest

from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.db import ConnectionConfig, ExplainError, fetch_plan_text, is_read_only
from sqlanalyzer.db import test_connection as check_connection
from sqlanalyzer.dialects import Engine


@pytest.fixture
def cfg(tmp_path):
    path = str(tmp_path / "t.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE TABLE t AS SELECT range a, range % 10 b FROM range(300000)")
    con.execute("CREATE TABLE u AS SELECT range a, range c FROM range(100)")
    con.close()
    return ConnectionConfig(Engine.DUCKDB, database=path)


def test_connection_and_urls(cfg):
    assert check_connection(cfg) == "Connection successful."
    snow = ConnectionConfig(Engine.SNOWFLAKE, host="acct", database="DB", schema="S", username="u", password="p",
                            extras={"warehouse": "W"})
    assert snow.display() == "snowflake://u:***@acct/DB/S?warehouse=W"
    assert ConnectionConfig(Engine.TRINO, host="h", database="hive", schema="s", username="u").display() == \
        "trino://u@h:8080/hive/s"
    assert ConnectionConfig(Engine.POSTGRES, host="h", database="d", username="u", password="p").display() == \
        "postgresql+psycopg2://u:***@h:5432/d"


def test_bad_connection():
    with pytest.raises(ExplainError):
        check_connection(ConnectionConfig(Engine.DUCKDB, database="/nonexistent_dir/x.duckdb"))


@pytest.mark.parametrize("analyze", [False, True])
def test_explain_roundtrip(cfg, analyze):
    sql = "SELECT t.a FROM t JOIN u ON t.a = u.a WHERE t.b > 3"
    res = analyze_query(sql, Engine.DUCKDB, plan_text=fetch_plan_text(cfg, sql, analyze))
    assert res.plan is not None and res.plan.analyzed is analyze
    kinds = [n.kind for n in res.plan.nodes()]
    assert "join" in kinds and kinds.count("scan") == 2
    assert {n.relation for n in res.plan.nodes() if n.relation} == {"memory.main.t", "memory.main.u"} or \
        any(n.relation and n.relation.endswith(".t") for n in res.plan.nodes())


def test_cross_product_detected_in_plan_and_diff(cfg):
    bad = "SELECT t.a FROM t, u"
    good = "SELECT t.a FROM t JOIN u ON t.a = u.a"
    a = analyze_query(bad, Engine.DUCKDB, plan_text=fetch_plan_text(cfg, bad))
    b = analyze_query(good, Engine.DUCKDB, plan_text=fetch_plan_text(cfg, good))
    assert "cartesian-product" in {f.rule for f in a.findings}
    cmp = compare_queries(a, b)
    assert cmp.plan_diff is not None and not cmp.plan_diff.identical
    assert any("cartesian" in f.rule for f in cmp.resolved)


def test_explain_analyze_refuses_writes(cfg):
    assert not is_read_only("DELETE FROM t", "duckdb")
    assert is_read_only("SELECT 1 UNION ALL SELECT 2", "duckdb")
    with pytest.raises(ExplainError):
        fetch_plan_text(cfg, "DELETE FROM t", analyze=True)
    with pytest.raises(ExplainError):
        fetch_plan_text(cfg, "SELECT * FROM missing_table")
