"""Per-rule tests for the optimization engine: each rule in isolation, positive and negative cases."""

import pytest
import sqlglot

from sqlanalyzer.optimizer import Optimizer, Rule, Safety, rule_by_name
from sqlanalyzer.optimizer.rules import RULE_CLASSES
from sqlanalyzer.parsing import parse_sql

SCHEMA = {
    "orders": {"id": "int", "customer_id": "int", "order_date": "date", "status": "text", "amount": "int",
               "s": "text", "ts": "timestamp"},
    "customers": {"id": "int", "name": "text", "region": "text"},
    "refunds": {"order_id": "int", "amt": "int"},
}


def run(sql: str, *rule_names: str, dialect: str = "duckdb", schema=None, include_review: bool = True,
        with_qualify: bool = False):
    """Optimize ``sql`` with only the named rules (plus ``qualify`` when requested)."""
    rules: list[Rule] = [rule_by_name(n) for n in rule_names]
    if with_qualify:
        rules.insert(0, rule_by_name("qualify"))
    parsed = parse_sql(sql, dialect)
    assert parsed.ok, parsed.errors
    return Optimizer(rules, include_review=include_review).optimize(parsed.statements, dialect, schema)


def rewritten(result) -> str:
    """Single-line rendering of the optimized statements."""
    assert result.changed, "expected a rewrite"
    assert not result.errors, result.errors
    return "; ".join(s.sql(dialect=result.dialect) for s in result.statements) + ";"


# ----------------------------------------------------------------------------- metadata
def test_every_rule_has_metadata_and_unique_name():
    names = [cls.name for cls in RULE_CLASSES]
    assert len(names) == len(set(names))
    for cls in RULE_CLASSES:
        assert cls.title and cls.description and cls.category
        assert isinstance(cls.safety, Safety)
        assert isinstance(cls(), Rule)


def test_rule_by_name_unknown():
    with pytest.raises(KeyError):
        rule_by_name("no-such-rule")


# ----------------------------------------------------------------- null comparison
def test_null_comparison_rewrites_eq_and_neq():
    r = run("SELECT a FROM t WHERE a = NULL AND NULL <> b AND c = 1", "null-comparison")
    assert rewritten(r) == "SELECT a FROM t WHERE a IS NULL AND NOT b IS NULL AND c = 1;"
    assert [rw.rule for rw in r.rewrites] == ["null-comparison"] * 2
    assert all(rw.safety is Safety.REVIEW for rw in r.rewrites)
    assert r.rewrites[0].before == "a = NULL" and r.rewrites[0].after == "a IS NULL"
    assert r.rewrites[0].line == 1 and r.rewrites[0].col == 23


def test_null_comparison_leaves_null_eq_null_and_is_null():
    assert not run("SELECT a FROM t WHERE NULL = NULL AND a IS NULL", "null-comparison").changed


def test_review_rules_are_skipped_when_review_is_off():
    r = run("SELECT a FROM t WHERE a = NULL", "null-comparison", include_review=False)
    assert not r.changed and "null-comparison" in r.skipped


# ------------------------------------------------------------------- sargable date
@pytest.mark.parametrize("pred, expected", [
    ("DATE(ts) = '2024-01-15'", "(ts >= CAST('2024-01-15' AS DATE) AND ts < CAST('2024-01-16' AS DATE))"),
    ("CAST(ts AS DATE) = '2024-12-31'", "(ts >= CAST('2024-12-31' AS DATE) AND ts < CAST('2025-01-01' AS DATE))"),
    ("'2024-01-15' = DATE(ts)", "(ts >= CAST('2024-01-15' AS DATE) AND ts < CAST('2024-01-16' AS DATE))"),
    ("DATE(ts) >= '2024-01-15'", "ts >= CAST('2024-01-15' AS DATE)"),
    ("DATE(ts) > '2024-01-15'", "ts >= CAST('2024-01-16' AS DATE)"),
    ("DATE(ts) < '2024-01-15'", "ts < CAST('2024-01-15' AS DATE)"),
    ("DATE(ts) <= '2024-01-15'", "ts < CAST('2024-01-16' AS DATE)"),
    ("'2024-01-15' < DATE(ts)", "ts >= CAST('2024-01-16' AS DATE)"),
    ("DATE(ts) BETWEEN '2024-01-15' AND '2024-01-16'",
     "(ts >= CAST('2024-01-15' AS DATE) AND ts < CAST('2024-01-17' AS DATE))"),
    ("YEAR(ts) = 2024", "(ts >= CAST('2024-01-01' AS DATE) AND ts < CAST('2025-01-01' AS DATE))"),
    ("EXTRACT(YEAR FROM ts) >= 2020", "ts >= CAST('2020-01-01' AS DATE)"),
    ("2024 > YEAR(ts)", "ts < CAST('2024-01-01' AS DATE)"),
])
def test_sargable_date_rewrites(pred, expected):
    r = run(f"SELECT id FROM orders WHERE {pred} AND id > 0", "sargable-date")
    assert rewritten(r) == f"SELECT id FROM orders WHERE {expected} AND id > 0;"
    assert r.rewrites[0].safety is Safety.REVIEW  # column type unknown without a schema


def test_sargable_date_is_safe_when_schema_confirms_type():
    r = run("SELECT o.id FROM orders o WHERE DATE(o.ts) = '2024-01-15'", "sargable-date", schema=SCHEMA)
    assert r.changed and r.rewrites[0].safety is Safety.SAFE
    r = run("SELECT o.id FROM orders o WHERE DATE(o.s) = '2024-01-15'", "sargable-date", schema=SCHEMA)
    assert r.changed and r.rewrites[0].safety is Safety.REVIEW  # s is text
    # without review rules the unknown-type case is not rewritten at all
    assert not run("SELECT id FROM orders WHERE DATE(ts) = '2024-01-15'", "sargable-date", include_review=False).changed
    assert run("SELECT o.id FROM orders o WHERE DATE(o.ts) = '2024-01-15'", "sargable-date", schema=SCHEMA,
               include_review=False).changed


@pytest.mark.parametrize("pred", [
    "DATE(ts) = 'not a date'", "YEAR(ts) = 99999", "DATE(ts) = current_date", "DATE(ts) <> '2024-01-01'",
    "DATE(ts) IN ('2024-01-01')", "DATE(ts + INTERVAL '1' DAY) = '2024-01-01'", "DATE(ts) = other_col",
])
def test_sargable_date_negative(pred):
    assert not run(f"SELECT id FROM orders WHERE {pred}", "sargable-date").changed


# ---------------------------------------------------------------------- OR -> IN
def test_or_to_in_merges_same_column():
    r = run("SELECT a FROM t WHERE a = 1 OR a = 2 OR a IN (3, 4) OR b = 5 OR 6 = a", "or-to-in")
    assert rewritten(r) == "SELECT a FROM t WHERE a IN (1, 2, 3, 4, 6) OR b = 5;"
    assert r.rewrites[0].fixes == ("or-across-columns",)


def test_or_to_in_keeps_parentheses_inside_and():
    r = run("SELECT a FROM t WHERE c = 1 AND (a = 'x' OR a = 'y' OR d = 2)", "or-to-in")
    assert rewritten(r) == "SELECT a FROM t WHERE c = 1 AND (a IN ('x', 'y') OR d = 2);"


@pytest.mark.parametrize("pred", ["a = 1 OR b = 2", "a = 1 OR a = b", "a = 1 OR a = NULL", "a = 1 OR a > 2",
                                  "a = 1 OR a IN (SELECT x FROM u)"])
def test_or_to_in_negative(pred):
    assert not run(f"SELECT a FROM t WHERE {pred}", "or-to-in").changed


# ------------------------------------------------------------------- single IN
def test_single_value_in():
    r = run("SELECT a FROM t WHERE a IN (1) AND b NOT IN ('x') AND c IN (1, 2)", "single-value-in")
    assert rewritten(r) == "SELECT a FROM t WHERE a = 1 AND b <> 'x' AND c IN (1, 2);"
    assert not run("SELECT a FROM t WHERE a IN (SELECT b FROM u)", "single-value-in").changed


# --------------------------------------------------------------------- LIKE / prefix
def test_like_without_wildcard_and_prefix_match():
    r = run("SELECT a FROM t WHERE s LIKE 'abc' AND s LIKE 'ab%' AND s LIKE 'a_c'", "like-to-equality")
    assert rewritten(r) == "SELECT a FROM t WHERE s = 'abc' AND s LIKE 'ab%' AND s LIKE 'a_c';"
    r = run("SELECT a FROM t WHERE SUBSTR(s, 1, 3) = 'abc' AND 'xy' = LEFT(s, 2)", "prefix-match")
    assert rewritten(r) == "SELECT a FROM t WHERE s LIKE 'abc%' AND s LIKE 'xy%';"
    for pred in ("SUBSTR(s, 2, 3) = 'abc'", "SUBSTR(s, 1, 2) = 'abc'", "SUBSTR(s, 1, 3) = 'a%c'",
                 "SUBSTR(lower(s), 1, 3) = 'abc'", "s LIKE 'a\\%' ESCAPE '\\'"):
        assert not run(f"SELECT a FROM t WHERE {pred}", "prefix-match", "like-to-equality").changed, pred


# ------------------------------------------------------------------------ simplify
def test_simplify_removes_tautologies_and_folds_constants():
    r = run("SELECT a FROM t WHERE 1 = 1 AND a + 0 > 10 AND NOT (b = 3) AND (c = 1 OR c = 1) AND TRUE", "simplify")
    assert rewritten(r) == "SELECT a FROM t WHERE a > 10 AND b <> 3 AND c = 1;"
    assert r.rewrites[0].description == "WHERE clause simplified."


def test_simplify_keeps_operand_order_and_between():
    sql = "SELECT a FROM t WHERE z = 1 AND y BETWEEN 1 AND 5 AND 'x' = w AND t.b = t.a AND 1 = 1"
    r = run(sql, "simplify")
    # conjunct order is preserved, BETWEEN is kept; constants move to the right-hand side
    assert rewritten(r) == "SELECT a FROM t WHERE z = 1 AND y BETWEEN 1 AND 5 AND w = 'x' AND t.b = t.a;"
    assert not run("SELECT a FROM t WHERE z = 1 AND y BETWEEN 1 AND 5 AND t.b = t.a", "simplify").changed
    # swapping operands alone is cosmetic and therefore not reported as a rewrite
    assert not run("SELECT a FROM t WHERE 'x' = w", "simplify").changed


def test_simplify_date_trunc_range():
    r = run("SELECT a FROM t WHERE DATE_TRUNC('DAY', ts) = CAST('2024-01-01' AS DATE)", "simplify")
    assert "ts >= CAST('2024-01-01' AS DATE) AND ts < CAST('2024-01-02' AS DATE)" in rewritten(r)


def test_simplify_removes_where_true():
    r = run("SELECT a FROM t WHERE TRUE AND 1 = 1", "simplify")
    assert rewritten(r) == "SELECT a FROM t;"
    assert r.rewrites[0].after == "(removed)"


# ------------------------------------------------------------------ explicit joins
def test_comma_join_becomes_explicit_join():
    sql = "SELECT o.id FROM orders o, customers c, refunds r WHERE o.customer_id = c.id AND r.order_id = o.id AND r.amt > 3"
    r = run(sql, "explicit-join")
    assert rewritten(r) == ("SELECT o.id FROM orders AS o JOIN customers AS c ON o.customer_id = c.id "
                            "JOIN refunds AS r ON r.order_id = o.id WHERE r.amt > 3;")
    assert [rw.fixes for rw in r.rewrites] == [("implicit-join", "cartesian-join")] * 2
    assert r.rewrites[0].line == 1 and r.rewrites[0].col == sql.index("customers") + 1


def test_comma_join_without_predicate_becomes_cross_join():
    r = run("SELECT o.id FROM orders o, customers c WHERE o.amount > 1", "explicit-join")
    assert rewritten(r) == "SELECT o.id FROM orders AS o CROSS JOIN customers AS c WHERE o.amount > 1;"
    assert "Cartesian" in r.rewrites[0].description


def test_comma_join_predicates_with_subqueries_stay_in_where():
    sql = "SELECT * FROM a, b WHERE a.x = b.x AND b.y = (SELECT MAX(z) FROM c WHERE c.k = a.k)"
    r = run(sql, "explicit-join")
    assert rewritten(r) == "SELECT * FROM a JOIN b ON a.x = b.x WHERE b.y = (SELECT MAX(z) FROM c WHERE c.k = a.k);"


def test_explicit_join_ignores_real_joins():
    assert not run("SELECT * FROM a JOIN b ON a.x = b.x WHERE a.y = 1", "explicit-join").changed
    assert not run("SELECT * FROM a CROSS JOIN b", "explicit-join").changed


# ---------------------------------------------------------------- LEFT -> INNER
@pytest.mark.parametrize("pred", ["b.y = 1", "b.y <> 1", "b.y > 1", "b.y IN (1, 2)", "b.y BETWEEN 1 AND 2",
                                  "b.y LIKE 'x%'", "b.y IS NOT NULL", "CAST(b.y AS INT) = 1", "b.y + 1 = 2",
                                  "1 = b.y"])
def test_left_join_becomes_inner_for_null_rejecting_predicates(pred):
    r = run(f"SELECT * FROM a LEFT JOIN b ON a.x = b.x WHERE {pred} AND a.q = 1", "left-join-to-inner")
    assert rewritten(r).startswith("SELECT * FROM a INNER JOIN b ON a.x = b.x WHERE")
    assert r.rewrites[0].fixes == ("left-join-filtered",)


@pytest.mark.parametrize("pred", ["b.y IS NULL", "COALESCE(b.y, 0) = 0", "b.y = 1 OR a.z = 2", "a.q = 1",
                                  "CASE WHEN b.y = 1 THEN 1 ELSE 0 END = 1", "NOT (b.y = 1)"])
def test_left_join_stays_left_for_null_tolerant_predicates(pred):
    assert not run(f"SELECT * FROM a LEFT JOIN b ON a.x = b.x WHERE {pred}", "left-join-to-inner").changed


def test_left_join_rule_only_handles_left_joins():
    assert not run("SELECT * FROM a RIGHT JOIN b ON a.x = b.x WHERE a.y = 1", "left-join-to-inner").changed
    assert not run("SELECT * FROM a FULL JOIN b ON a.x = b.x WHERE b.y = 1", "left-join-to-inner").changed


# ------------------------------------------------------- duplicate join predicate
def test_duplicate_join_predicate_removed_from_where():
    sql = "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.id = o.customer_id AND c.region = 'x'"
    r = run(sql, "duplicate-join-predicate")
    assert rewritten(r) == ("SELECT o.id FROM orders AS o JOIN customers AS c ON o.customer_id = c.id "
                            "WHERE c.region = 'x';")
    # never for outer joins (the WHERE predicate changes the semantics there)
    assert not run("SELECT o.id FROM orders o LEFT JOIN customers c ON o.customer_id = c.id WHERE c.id = o.customer_id",
                   "duplicate-join-predicate").changed


# ------------------------------------------------------------------ HAVING / DISTINCT
def test_having_to_where_moves_only_group_by_predicates():
    sql = "SELECT status, COUNT(*) AS n FROM orders GROUP BY status HAVING status <> 'a' AND COUNT(*) > 1"
    r = run(sql, "having-to-where")
    assert rewritten(r) == ("SELECT status, COUNT(*) AS n FROM orders WHERE status <> 'a' GROUP BY status "
                            "HAVING COUNT(*) > 1;")
    r = run("SELECT status FROM orders GROUP BY status HAVING status <> 'a'", "having-to-where")
    assert rewritten(r) == "SELECT status FROM orders WHERE status <> 'a' GROUP BY status;"


@pytest.mark.parametrize("sql", [
    "SELECT a, b FROM t GROUP BY 1, 2 HAVING a > 1",  # ordinal GROUP BY
    "SELECT a AS b, COUNT(*) FROM t GROUP BY a HAVING b > 1",  # alias, not a group column
    "SELECT COUNT(*) AS a FROM t GROUP BY a HAVING a > 1",  # alias shadows the column
    "SELECT a FROM t GROUP BY a HAVING b > 1",  # not a GROUP BY column
    "SELECT a FROM t GROUP BY a HAVING a IN (SELECT x FROM u)",  # subquery
])
def test_having_to_where_negative(sql):
    assert not run(sql, "having-to-where").changed


def test_redundant_distinct():
    r = run("SELECT DISTINCT status, COUNT(*) AS n FROM orders GROUP BY status", "redundant-distinct")
    assert rewritten(r) == "SELECT status, COUNT(*) AS n FROM orders GROUP BY status;"
    assert not run("SELECT DISTINCT status FROM orders GROUP BY status, amount", "redundant-distinct").changed
    assert not run("SELECT DISTINCT status FROM orders", "redundant-distinct").changed


# ------------------------------------------------------------------ subqueries
def test_not_in_to_not_exists():
    r = run("SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT r.order_id FROM refunds r WHERE r.amt > 0)",
            "not-in-to-not-exists")
    assert rewritten(r) == ("SELECT o.id FROM orders AS o WHERE NOT EXISTS(SELECT 1 FROM refunds AS r "
                            "WHERE r.amt > 0 AND r.order_id = o.id);")
    assert r.rewrites[0].safety is Safety.REVIEW and r.rewrites[0].fixes == ("not-in-subquery",)


@pytest.mark.parametrize("sql", [
    "SELECT id FROM orders WHERE customer_id NOT IN (SELECT id FROM customers)",  # unqualified outer column
    "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT x FROM u GROUP BY x)",
    "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT DISTINCT x FROM u)",
    "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT x FROM u LIMIT 5)",
    "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT MAX(x) FROM u)",
    "SELECT o.id FROM orders o WHERE o.id NOT IN (SELECT x FROM u JOIN v ON u.k = v.k)",  # ambiguous column
    "SELECT o.id FROM orders o WHERE (o.id, o.x) NOT IN (SELECT a, b FROM u)",
    "SELECT o.id FROM orders o WHERE o.id NOT IN (1, 2)",
])
def test_not_in_to_not_exists_negative(sql):
    assert not run(sql, "not-in-to-not-exists").changed


def test_subquery_order_by_removed():
    r = run("SELECT x.id FROM (SELECT id FROM orders ORDER BY amount) x", "subquery-order-by")
    assert rewritten(r) == "SELECT x.id FROM (SELECT id FROM orders) AS x;"
    r = run("WITH c AS (SELECT id FROM orders ORDER BY id) SELECT id FROM c WHERE id IN (SELECT order_id FROM refunds ORDER BY 1)",
            "subquery-order-by")
    assert rewritten(r) == "WITH c AS (SELECT id FROM orders) SELECT id FROM c WHERE id IN (SELECT order_id FROM refunds);"
    assert all(rw.safety is Safety.REVIEW for rw in r.rewrites)


@pytest.mark.parametrize("sql", [
    "SELECT x.id FROM (SELECT id FROM orders ORDER BY amount LIMIT 5) x",
    "SELECT x.id FROM (SELECT id FROM orders ORDER BY amount OFFSET 5) x",
    "SELECT id FROM orders ORDER BY amount",  # outermost query
    "SELECT (SELECT id FROM orders ORDER BY amount LIMIT 1) AS first",
    "SELECT x.id FROM (SELECT id, ROW_NUMBER() OVER () AS rn FROM orders ORDER BY amount) x",
    "SELECT x.ids FROM (SELECT ARRAY_AGG(id) AS ids FROM orders ORDER BY amount) x",
])
def test_subquery_order_by_negative(sql):
    assert not run(sql, "subquery-order-by").changed


def test_unused_cte_removed_transitively():
    sql = "WITH a AS (SELECT 1 AS x), b AS (SELECT x FROM a), c AS (SELECT 2 AS y) SELECT y FROM c"
    r = run(sql, "unused-cte")
    assert rewritten(r) == "WITH c AS (SELECT 2 AS y) SELECT y FROM c;"
    assert sorted(rw.before.split(" AS ")[0] for rw in r.rewrites) == ["a", "b"]
    r = run("WITH a AS (SELECT 1 AS x) SELECT 2", "unused-cte")
    assert rewritten(r) == "SELECT 2;"


def test_unused_cte_keeps_referenced_and_recursive_ctes():
    assert not run("WITH a AS (SELECT 1 AS x) SELECT x FROM a", "unused-cte").changed
    assert not run("WITH a AS (SELECT 1 AS x) SELECT * FROM t WHERE t.x IN (SELECT x FROM a)", "unused-cte").changed
    sql = "WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) SELECT n FROM r"
    assert not run(sql, "unused-cte").changed
    assert run("WITH a AS (SELECT 1 AS x) SELECT x FROM db.a", "unused-cte").changed  # db.a is a real table


def test_distinct_in_semijoin_and_exists_projection():
    sql = ("SELECT id FROM orders o WHERE o.customer_id IN (SELECT DISTINCT id FROM customers) "
           "AND EXISTS (SELECT DISTINCT * FROM refunds r WHERE r.order_id = o.id)")
    r = run(sql, "distinct-in-semijoin", "exists-select-1")
    assert rewritten(r) == ("SELECT id FROM orders AS o WHERE o.customer_id IN (SELECT id FROM customers) "
                            "AND EXISTS(SELECT 1 FROM refunds AS r WHERE r.order_id = o.id);")
    assert not run("SELECT 1 WHERE EXISTS (SELECT MAX(x) FROM t)", "exists-select-1").changed
    assert not run("SELECT 1 WHERE EXISTS (SELECT x FROM t GROUP BY x)", "exists-select-1").changed
    assert not run("SELECT 1 WHERE EXISTS (SELECT 1 FROM t)", "exists-select-1").changed
    assert not run("SELECT 1 WHERE x IN (SELECT DISTINCT y FROM t LIMIT 3)", "distinct-in-semijoin").changed


# --------------------------------------------------------------------- set ops
def test_union_all():
    r = run("SELECT a FROM t UNION SELECT b FROM u UNION ALL SELECT c FROM v", "union-all")
    assert rewritten(r) == "SELECT a FROM t UNION ALL SELECT b FROM u UNION ALL SELECT c FROM v;"
    assert len(r.rewrites) == 1 and r.rewrites[0].safety is Safety.REVIEW
    assert not run("SELECT a FROM t INTERSECT SELECT b FROM u", "union-all").changed


# ------------------------------------------------------------ qualify / star expansion
def test_qualify_expands_star_with_schema():
    r = run("SELECT * FROM orders WHERE status = 'a'", with_qualify=True, schema=SCHEMA)
    assert rewritten(r) == ("SELECT orders.id, orders.customer_id, orders.order_date, orders.status, orders.amount, "
                            "orders.s, orders.ts FROM orders WHERE orders.status = 'a';")
    assert r.rewrites[0].rule == "qualify" and r.rewrites[0].fixes == ("select-star",)
    assert "7 column(s)" in r.rewrites[0].description and r.qualified


def test_qualify_without_schema_needs_star_free_qualified_sql():
    r = run("SELECT * FROM orders WHERE status = 'a'", with_qualify=True)
    assert not r.changed and not r.qualified
    r = run("SELECT id FROM orders o JOIN customers c ON o.customer_id = c.id", with_qualify=True)
    assert not r.qualified  # `id` cannot be attributed to a table
    r = run("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", with_qualify=True)
    assert r.qualified and not r.changed  # qualification alone is not reported as a rewrite


def test_qualify_fails_for_columns_missing_from_the_schema():
    r = run("SELECT o.nope FROM orders o", with_qualify=True, schema=SCHEMA)
    assert not r.qualified and not r.changed and not r.errors


# ------------------------------------------------------------- sqlglot passes
def test_pushdown_predicates_and_projections_into_cte():
    sql = ("WITH o AS (SELECT id, amount, customer_id, status FROM orders) "
           "SELECT o.id FROM o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'x'")
    r = run(sql, "pushdown-projections", "pushdown-predicates", with_qualify=True, schema=SCHEMA)
    out = rewritten(r)
    # the filter moved into the CTE, after which only id/customer_id are needed outside
    assert "WITH o AS (SELECT orders.id, orders.customer_id FROM orders WHERE orders.status = 'x')" in out
    assert "amount" not in out
    rules = [rw.rule for rw in r.rewrites]
    assert "pushdown-projections" in rules and "pushdown-predicates" in rules
    assert any("pushed into" in rw.description for rw in r.rewrites)


def test_sqlglot_passes_are_skipped_without_qualification():
    r = run("SELECT * FROM (SELECT id, amount FROM orders) s WHERE s.id > 1", "pushdown-predicates", "merge-subqueries")
    assert not r.changed
    assert set(r.skipped) == {"pushdown-predicates", "merge-subqueries"}
    assert "schema" in r.skipped["merge-subqueries"]


def test_merge_subqueries():
    r = run("SELECT s.id FROM (SELECT id, amount FROM orders WHERE amount > 5) s WHERE s.id > 10",
            "merge-subqueries", with_qualify=True, schema=SCHEMA)
    out = rewritten(r)
    assert out.startswith("SELECT orders.id FROM orders WHERE") and "(SELECT" not in out
    assert "orders.amount > 5" in out and "orders.id > 10" in out
    assert "merged into the outer query" in r.rewrites[0].description


def test_dedupe_subqueries_only_reports_real_duplicates():
    sql = ("SELECT a.id FROM (SELECT id FROM orders WHERE status = 'a') a "
           "JOIN (SELECT id FROM orders WHERE status = 'a') b ON a.id = b.id")
    r = run(sql, "dedupe-subqueries", with_qualify=True, schema=SCHEMA)
    assert r.changed and r.rewrites[0].rule == "dedupe-subqueries"
    assert rewritten(r).count("WITH") == 1 and "FROM orders" in rewritten(r)
    r = run("SELECT s.id FROM (SELECT id FROM orders) s", "dedupe-subqueries", with_qualify=True, schema=SCHEMA)
    assert not r.changed  # a single derived table turned into a CTE is not an optimization


def test_eliminate_joins():
    sql = ("SELECT o.id FROM orders o LEFT JOIN (SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id) x "
           "ON x.customer_id = o.customer_id")
    r = run(sql, "eliminate-joins", with_qualify=True, schema=SCHEMA)
    assert rewritten(r) == "SELECT o.id FROM orders AS o;"


def test_optimize_joins_reorders_by_dependency():
    sql = ("SELECT o.id FROM orders o CROSS JOIN customers c JOIN refunds r "
           "ON o.id = r.order_id AND c.id = r.amt")
    r = run(sql, "optimize-joins", with_qualify=True, schema=SCHEMA)
    assert r.changed and "CROSS JOIN" not in rewritten(r)
    assert "JOIN customers AS c ON c.id = r.amt" in rewritten(r)


def test_unnest_subqueries_is_opt_in_and_reviewed():
    from sqlanalyzer.optimizer import OPT_IN_RULES, default_rules

    assert "unnest-subqueries" in OPT_IN_RULES
    assert "unnest-subqueries" not in {r.name for r in default_rules()}
    assert "unnest-subqueries" in {r.name for r in default_rules(include_opt_in=True)}
    sql = "SELECT o.id FROM orders o WHERE o.customer_id IN (SELECT c.id FROM customers c WHERE c.region = 'EU')"
    r = run(sql, "unnest-subqueries", with_qualify=True, schema=SCHEMA)
    assert r.changed and r.rewrites[0].safety is Safety.REVIEW and "JOIN" in rewritten(r)


# ------------------------------------------------------------------ dialects
@pytest.mark.parametrize("dialect", ["postgres", "duckdb", "trino", "snowflake"])
def test_rules_produce_parseable_sql_in_every_dialect(dialect):
    sql = ("SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id AND DATE(o.ts) = '2024-01-15' "
           "AND (o.status = 'a' OR o.status = 'b') AND o.x = NULL AND o.id NOT IN (SELECT r.order_id FROM refunds r) "
           "AND SUBSTR(o.s, 1, 2) = 'ab' AND 1 = 1")
    r = run(sql, *[cls.name for cls in RULE_CLASSES], dialect=dialect)
    assert r.changed and not r.errors
    assert {"explicit-join", "sargable-date", "or-to-in", "null-comparison", "not-in-to-not-exists", "prefix-match",
            "simplify"} <= {rw.rule for rw in r.rewrites}
    assert sqlglot.parse_one(r.optimized_sql, read=dialect) is not None
