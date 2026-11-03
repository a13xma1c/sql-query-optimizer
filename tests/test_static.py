import pytest

from sqlanalyzer.parsing import format_sql, parse_sql
from sqlanalyzer.static import analyze_static


def rules(sql: str, dialect: str = "postgres") -> set[str]:
    parsed = parse_sql(sql, dialect)
    assert parsed.ok, parsed.errors
    return {f.rule for f in analyze_static(parsed.statements)}


@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "trino", "snowflake"])
def test_valid_sql_parses_in_every_dialect(dialect):
    assert parse_sql("SELECT a, count(*) FROM t WHERE b > 1 GROUP BY a", dialect).ok


def test_syntax_error_reports_position():
    res = parse_sql("SELECT a FROM t WHERE (x = 1 AND\nSELECT FROM", "trino")
    assert not res.ok
    err = res.errors[0]
    assert err.rule == "syntax-error" and err.line == 2


def test_unterminated_string_is_reported():
    res = parse_sql("SELECT 'abc FROM t", "postgres")
    assert res.errors[0].rule == "tokenize-error" and res.errors[0].line == 1


def test_empty_query():
    assert parse_sql("   ", "postgres").errors[0].rule == "empty-query"


def test_formatting_is_idempotent_and_multiline():
    once = format_sql("select a,b from t where x=1;select 2", "postgres")
    assert "\n" in once and once.count(";") == 2
    assert format_sql(once, "postgres") == once


def test_select_star_and_count_star():
    assert "select-star" in rules("SELECT * FROM t WHERE a = 1")
    assert "select-star" not in rules("SELECT count(*) FROM t WHERE a = 1")
    assert "select-star" not in rules("SELECT a FROM t WHERE EXISTS (SELECT * FROM u WHERE u.a = t.a)")


def test_lint_rules():
    r = rules("SELECT a FROM t WHERE b = NULL AND c NOT IN (SELECT c FROM u WHERE x = 1) AND d LIKE '%x' ORDER BY 1")
    assert {"null-comparison", "not-in-subquery", "leading-wildcard", "order-by-ordinal"} <= r
    assert "union-distinct" in rules("SELECT a FROM t WHERE a=1 UNION SELECT a FROM u WHERE a=1")
    assert "missing-where" in rules("DELETE FROM t")
    assert "limit-without-order" in rules("SELECT a FROM t WHERE a = 1 LIMIT 5")


def test_cartesian_and_implicit_joins():
    assert "cartesian-join" in rules("SELECT a.x FROM a, b WHERE a.x = 1")
    assert "cartesian-join" in rules("SELECT a.x FROM a CROSS JOIN b WHERE a.x = 1")
    r = rules("SELECT a.x FROM a, b WHERE a.id = b.id AND a.x = 1")
    assert "implicit-join" in r and "cartesian-join" not in r


def test_join_quality_rules():
    assert "non-equi-join" in rules("SELECT 1 FROM a JOIN b ON a.x > b.x WHERE a.k = 1")
    assert "or-in-join" in rules("SELECT 1 FROM a JOIN b ON a.x = b.x OR a.y = b.y WHERE a.k = 1")
    assert "join-on-expression" in rules("SELECT 1 FROM a JOIN b ON lower(a.k) = b.k WHERE a.k = 1")
    assert "left-join-filtered" in rules("SELECT 1 FROM a LEFT JOIN b ON a.i = b.i WHERE b.z = 3")
    assert "left-join-filtered" not in rules("SELECT 1 FROM a LEFT JOIN b ON a.i = b.i AND b.z = 3 WHERE a.k = 1")
    assert "many-joins" in rules(
        "SELECT 1 FROM t0 " + " ".join(f"JOIN t{i} ON t{i - 1}.id = t{i}.id" for i in range(1, 8)) + " WHERE t0.id = 1")


def test_correlated_subquery_in_select():
    assert "correlated-subquery" in rules("SELECT (SELECT max(q.v) FROM q WHERE q.i = a.i) FROM a WHERE a.k = 1")


def test_scan_rules():
    assert "unfiltered-scan" in rules("SELECT a FROM t")
    assert "unfiltered-scan" not in rules("SELECT count(*) FROM t")
    assert "unfiltered-scan" not in rules("SELECT a FROM t LIMIT 10")
    assert "non-sargable-predicate" in rules("SELECT a FROM t WHERE date(ts) = '2024-01-01'")
    assert "non-sargable-predicate" not in rules("SELECT a FROM t WHERE ts >= '2024-01-01' AND ts < '2024-01-02'")
    assert "non-sargable-predicate" in rules("SELECT a FROM t WHERE a + 1 = 5")
    assert "repeated-scan" in rules("SELECT 1 FROM t x JOIN t y ON x.a = y.a WHERE x.b = 1")
    assert "or-across-columns" in rules("SELECT a FROM t WHERE a = 1 OR b = 2")
