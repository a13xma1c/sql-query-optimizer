"""Individual rewrite suggestions and selective application."""

import duckdb

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import Engine
from sqlanalyzer.optimizer import Safety, apply_rules, parse_schema, suggest, suggestions_for
from sqlanalyzer.parsing import parse_sql

SQL = ("SELECT * FROM orders o, customers c WHERE o.customer_id = c.id AND DATE(o.ts) = '2024-01-15' "
       "AND o.id NOT IN (SELECT order_id FROM refunds) AND (o.s = 'a' OR o.s = 'b') AND 1 = 1")


def statements(sql=SQL, dialect="postgres"):
    parsed = parse_sql(sql, dialect)
    assert parsed.ok
    return parsed.statements


def test_one_suggestion_per_applicable_rule():
    out = suggest(statements(), "postgres")
    rules = [s.rule for s in out]
    assert {"sargable-date", "explicit-join", "not-in-to-not-exists", "or-to-in", "simplify"} <= set(rules)
    assert len(rules) == len(set(rules))
    for s in out:
        assert s.rewrites and all(rw.rule == s.rule for rw in s.rewrites)
        assert s.sql.startswith("SELECT") and s.title and s.description and "``" not in s.description
        assert s.line == 1 and "->" in s.summary


def test_each_suggestion_applies_only_its_own_rule():
    out = {s.rule: s for s in suggest(statements(), "postgres")}
    explicit = " ".join(out["explicit-join"].sql.split())
    assert "JOIN customers AS c ON o.customer_id = c.id" in explicit
    assert "DATE(o.ts) = '2024-01-15'" in explicit and "NOT o.id IN" in explicit  # other rules untouched
    sargable = " ".join(out["sargable-date"].sql.split())
    assert "orders AS o, customers AS c" in sargable and "o.ts >= CAST('2024-01-15' AS DATE)" in sargable


def test_safety_reflects_the_actual_rewrites():
    out = {s.rule: s for s in suggest(statements(), "postgres")}
    assert out["sargable-date"].safety is Safety.REVIEW  # column type unknown without a schema
    assert out["not-in-to-not-exists"].safety is Safety.REVIEW and out["explicit-join"].safety is Safety.SAFE
    schema = parse_schema("orders: id int, customer_id int, ts timestamp, s text\ncustomers: id int\nrefunds: order_id int")
    with_schema = {s.rule: s for s in suggest(statements(), "postgres", schema=schema.mapping())}
    assert with_schema["sargable-date"].safety is Safety.SAFE


def test_review_and_disabled_filters():
    safe_only = {s.rule for s in suggest(statements(), "postgres", include_review=False)}
    assert "not-in-to-not-exists" not in safe_only and "explicit-join" in safe_only
    assert "or-to-in" not in {s.rule for s in suggest(statements(), "postgres", disabled=["or-to-in"])}
    assert "approx-count-distinct" not in {s.rule for s in suggest(statements("SELECT count(DISTINCT x) FROM t"), "postgres")}
    assert "approx-count-distinct" in {s.rule for s in suggest(statements("SELECT count(DISTINCT x) FROM t", "trino"), "trino")}


def test_suggestions_address_findings():
    a = analyze_query(SQL, Engine.POSTGRES)
    non_sargable = next(f for f in a.findings if f.rule == "non-sargable-predicate")
    assert [s.rule for s in a.suggestions_for(non_sargable)] == ["sargable-date"]
    assert [s.rule for s in suggestions_for(a.suggestions, "not-in-subquery")] == ["not-in-to-not-exists"]
    assert suggestions_for(a.suggestions, "select-star") == []


def test_apply_rules_applies_exactly_the_chosen_subset():
    result = apply_rules(statements(), "postgres", ["explicit-join", "or-to-in"])
    assert {rw.rule for rw in result.rewrites} == {"explicit-join", "or-to-in"}
    flat = " ".join(result.optimized_sql.split())
    assert "JOIN customers AS c" in flat and "o.s IN ('a', 'b')" in flat and "1 = 1" in flat
    assert not apply_rules(statements(), "postgres", ["union-all"]).changed
    assert apply_rules(statements(), "postgres", ["simplify"], include_review=False).changed


def test_selected_rewrites_return_the_same_rows():
    con = duckdb.connect()
    con.execute("CREATE TABLE orders AS SELECT * FROM (VALUES (1, 1, 'a', TIMESTAMP '2024-01-15 10:00:00'),"
                " (2, 2, 'b', TIMESTAMP '2024-01-16 10:00:00'), (3, 1, 'c', TIMESTAMP '2024-01-15 23:00:00')) v(id, customer_id, s, ts);"
                "CREATE TABLE customers AS SELECT * FROM (VALUES (1,), (2,)) v(id);"
                "CREATE TABLE refunds AS SELECT * FROM (VALUES (3,)) v(order_id)")
    sql = SQL.replace("DATE(o.ts) = '2024-01-15'", "CAST(o.ts AS DATE) = DATE '2024-01-15'")
    baseline = sorted(con.execute(sql).fetchall())
    for s in suggest(statements(sql, "duckdb"), "duckdb"):
        assert sorted(con.execute(s.sql).fetchall()) == baseline, s.rule
    chosen = apply_rules(statements(sql, "duckdb"), "duckdb", ["explicit-join", "or-to-in", "sargable-date"])
    assert sorted(con.execute(chosen.optimized_sql).fetchall()) == baseline
    con.close()
