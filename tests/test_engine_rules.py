"""Engine-specific static rules and rewrite rules."""

import pytest

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.models import Category, Severity
from sqlanalyzer.optimizer import Safety, optimize_query, parse_schema
from sqlanalyzer.optimizer.rules import ENGINE_SPECIFIC_RULES
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.static_engine import ENGINE_RULES, analyze_engine_specific, rules_for

STATS = parse_schema("orders (rows=2000000): id int\ncustomers (rows=50000): id int").stats


def findings(sql: str, engine: Engine, stats=None):
    dialect = ENGINES[engine].sqlglot_dialect
    parsed = parse_sql(sql, dialect)
    assert parsed.ok, parsed.errors
    return {f.rule: f for f in analyze_engine_specific(parsed.statements, engine, stats)}


def test_every_rule_targets_at_least_one_engine_and_has_a_description():
    for rule in ENGINE_RULES:
        assert rule.engines and rule.description and rule.name
    assert {r.name for r in rules_for(Engine.TRINO)} >= {"count-distinct-exact", "join-build-side", "columnar-select-star"}
    assert "cte-materialized" not in {r.name for r in rules_for(Engine.TRINO)}
    assert "variant-field-filter" in {r.name for r in rules_for(Engine.SNOWFLAKE)}


# ------------------------------------------------------------ distributed engines
@pytest.mark.parametrize("engine", [Engine.TRINO, Engine.SNOWFLAKE])
def test_count_distinct_and_global_sort(engine):
    f = findings("SELECT count(DISTINCT x) FROM t JOIN u ON t.id = u.id ORDER BY 1", engine)
    assert "count-distinct-exact" in f and ("APPROX_DISTINCT" if engine is Engine.TRINO else "APPROX_COUNT_DISTINCT") \
        in f["count-distinct-exact"].suggestion
    assert "global-sort-without-limit" in f
    assert "global-sort-without-limit" not in findings("SELECT x FROM t JOIN u ON t.id = u.id ORDER BY 1 LIMIT 5", engine)
    assert "count-distinct-exact" not in findings("SELECT count(DISTINCT x) FROM t", Engine.POSTGRES)


def test_trino_join_build_side_uses_statistics():
    sql = "SELECT 1 FROM customers c JOIN orders o ON o.customer_id = c.id"
    assert "join-build-side" in findings(sql, Engine.TRINO, STATS)
    assert "join-build-side" not in findings(sql, Engine.TRINO)  # no statistics: no claim
    assert "join-build-side" not in findings("SELECT 1 FROM orders o JOIN customers c ON o.customer_id = c.id",
                                             Engine.TRINO, STATS)


def test_columnar_select_star_is_a_warning_only_on_columnar_engines():
    for engine in (Engine.DUCKDB, Engine.TRINO, Engine.SNOWFLAKE):
        f = findings("SELECT * FROM t WHERE x = 1", engine)
        assert f["columnar-select-star"].severity is Severity.WARNING
    assert "columnar-select-star" not in findings("SELECT * FROM t WHERE x = 1", Engine.POSTGRES)
    assert "columnar-select-star" not in findings("SELECT a FROM (SELECT * FROM t) s", Engine.DUCKDB)  # inner


def test_snowflake_variant_filter():
    f = findings("SELECT v:name FROM t WHERE v:status = 'a'", Engine.SNOWFLAKE)
    assert f["variant-field-filter"].severity is Severity.WARNING
    assert "variant-field-filter" not in findings("SELECT v:name FROM t WHERE x = 1", Engine.SNOWFLAKE)


# --------------------------------------------------------------- row stores
def test_postgres_specific_rules():
    f = findings("WITH c AS (SELECT a FROM t) SELECT DISTINCT ON (a) a FROM c JOIN c c2 ON c.a = c2.a "
                 "WHERE lower(n) LIKE 'a%' AND CAST(ts AS VARCHAR) = '2024' AND s ~ 'abc' LIMIT 10 OFFSET 5000",
                 Engine.POSTGRES)
    assert {"cte-materialized", "distinct-on-without-order", "case-insensitive-like", "cast-column-to-string",
            "unanchored-regex", "large-offset"} <= set(f)
    assert f["large-offset"].severity is Severity.WARNING and "keyset" in f["large-offset"].suggestion
    assert "pg_trgm" in f["case-insensitive-like"].suggestion and "NOT MATERIALIZED" in f["cte-materialized"].suggestion
    assert "cte-materialized" not in findings("WITH c AS (SELECT a FROM t) SELECT a FROM c", Engine.POSTGRES)
    assert "unanchored-regex" not in findings("SELECT 1 FROM t WHERE s ~ '^abc'", Engine.POSTGRES)
    assert "distinct-on-without-order" not in findings("SELECT DISTINCT ON (a) a FROM t ORDER BY a, b", Engine.POSTGRES)


def test_long_in_list_thresholds():
    long_list = "SELECT 1 FROM t WHERE x IN (" + ",".join(map(str, range(150))) + ")"
    assert "ANY(ARRAY" in findings(long_list, Engine.POSTGRES)["long-in-list"].suggestion
    assert "long-in-list" not in findings(long_list, Engine.TRINO)  # Trino's threshold is 1000
    huge = "SELECT 1 FROM t WHERE x IN (" + ",".join(map(str, range(1200))) + ")"
    assert "VALUES" in findings(huge, Engine.SNOWFLAKE)["long-in-list"].suggestion


def test_large_offset_severity_by_engine():
    sql = "SELECT a FROM t ORDER BY a LIMIT 10 OFFSET 5000"
    assert findings(sql, Engine.DUCKDB)["large-offset"].severity is Severity.WARNING
    assert findings(sql, Engine.TRINO)["large-offset"].severity is Severity.INFO
    assert "large-offset" not in findings("SELECT a FROM t ORDER BY a LIMIT 10 OFFSET 20", Engine.DUCKDB)


def test_engine_findings_are_part_of_the_analysis():
    a = analyze_query("SELECT count(DISTINCT x) FROM t ORDER BY 1 OFFSET 5000", Engine.SNOWFLAKE, optimize=False)
    engine_findings = a.by_category(Category.ENGINE)
    assert {f.rule for f in engine_findings} >= {"count-distinct-exact", "large-offset"}
    assert all(f.line == 1 for f in engine_findings)


# ------------------------------------------------------------- rewrite rules
def test_engine_specific_rewrite_rules_declare_their_engines():
    for cls in ENGINE_SPECIFIC_RULES:
        assert cls.engines and cls.fixes


def test_approx_count_distinct_rewrite():
    sql = "SELECT count(DISTINCT x), count(DISTINCT a, b) FROM t"
    trino = optimize_query(sql, "trino")
    assert [rw.rule for rw in trino.rewrites] == ["approx-count-distinct"]  # the two-column DISTINCT is left alone
    assert trino.rewrites[0].safety is Safety.REVIEW and "APPROX_DISTINCT(x)" in trino.optimized_sql
    assert "APPROX_COUNT_DISTINCT(x)" in optimize_query(sql, "snowflake").optimized_sql
    pg = optimize_query(sql, "postgres")
    assert not pg.changed and "not applicable to postgres" in pg.skipped["approx-count-distinct"]


def test_ilike_rewrite():
    r = optimize_query("SELECT 1 FROM t WHERE lower(n) LIKE 'abc%' AND upper(m) LIKE 'X_' AND lower(k) LIKE 'Mixed'",
                       "postgres")
    assert "n ILIKE 'abc%'" in r.optimized_sql and "m ILIKE 'X_'" in r.optimized_sql
    assert "LOWER(k) = 'Mixed'" in r.optimized_sql  # not lower-case: left for like-to-equality, never ILIKE
    assert "ilike" in {rw.rule for rw in r.rewrites} and all("k" not in rw.before for rw in r.rewrites if rw.rule == "ilike")
    assert not optimize_query("SELECT 1 FROM t WHERE lower(n) LIKE 'abc%'", "trino").changed
    assert optimize_query("SELECT 1 FROM t WHERE lower(n) LIKE 'abc%'", "duckdb").rewrites[0].safety is Safety.REVIEW


def test_in_list_to_any_rewrite():
    values = ",".join(map(str, range(120)))
    r = optimize_query(f"SELECT 1 FROM t WHERE x IN ({values}) AND y IN (1, 2) AND z NOT IN ({values})", "postgres")
    assert [rw.rule for rw in r.rewrites] == ["in-list-to-any"]
    flat = " ".join(r.optimized_sql.split())
    assert "x = ANY( ARRAY[" in flat and "y IN (1, 2)" in flat and "NOT z IN (" in flat
    assert "120 values" in r.rewrites[0].before and r.rewrites[0].safety is Safety.SAFE
    assert not optimize_query(f"SELECT 1 FROM t WHERE x IN ({values})", "duckdb").changed


def test_engine_specific_rewrites_round_trip_on_duckdb():
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES ('Abc', 1), ('abd', 2), ('xyz', 3), (NULL, 4)) v(n, x)")
    sql = "SELECT x FROM t WHERE lower(n) LIKE 'ab%' ORDER BY x"
    rewritten = optimize_query(sql, "duckdb").optimized_sql
    assert "ILIKE" in rewritten
    assert con.execute(rewritten).fetchall() == con.execute(sql).fetchall() == [(1,), (2,)]
    con.close()
