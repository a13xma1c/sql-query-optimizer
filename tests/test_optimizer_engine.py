"""Engine-level tests: pass loop, validation, safety handling, schema hints and analyzer integration."""

import pytest
import sqlglot
from sqlglot import exp

from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.dialects import Engine
from sqlanalyzer.optimizer import (
    OptimizationResult,
    Optimizer,
    Rewrite,
    Rule,
    RuleContext,
    Safety,
    default_rules,
    optimize_query,
    parse_schema,
    rule_by_name,
)
from sqlanalyzer.optimizer.engine import canonical_sql, invalid_references, strip_qualification_noise, validate
from sqlanalyzer.parsing import parse_sql

SCHEMA = {
    "orders": {"id": "int", "customer_id": "int", "order_date": "date", "status": "text", "amount": "int"},
    "customers": {"id": "int", "name": "text", "region": "text"},
    "refunds": {"order_id": "int", "amt": "int"},
}
MESSY = """
SELECT o.id, c.name
FROM orders o, customers c
WHERE o.customer_id = c.id
  AND DATE(o.order_date) = '2024-01-15'
  AND o.id NOT IN (SELECT r.order_id FROM refunds r)
  AND 1 = 1
  AND (o.status = 'a' OR o.status = 'b' OR o.status = 'c')
  AND o.amount = NULL
"""


# ----------------------------------------------------------------------- helpers
class _BrokenRule(Rule):
    """Claims a rewrite but produces a dangling table reference."""

    name = "broken"
    title = "Broken"
    description = "test"
    category = "test"

    def apply(self, expression, ctx):
        where = expression.find(exp.Where)
        where.replace(exp.Where(this=exp.condition("zzz.col = 1")))
        return expression, [self.rewrite("x", "zzz.col = 1", "bogus", ctx)]


class _RaisingRule(Rule):
    name = "raising"
    title = "Raising"
    description = "test"
    category = "test"

    def apply(self, expression, ctx):
        raise RuntimeError("boom")


class _FlipFlopRule(Rule):
    """Swaps the two select columns every time it runs: must not loop forever."""

    name = "flipflop"
    title = "Flip"
    description = "test"
    category = "test"

    def apply(self, expression, ctx):
        sel = expression.find(exp.Select)
        a, b = sel.expressions
        sel.set("expressions", [b.copy(), a.copy()])
        return expression, [self.rewrite("a, b", "b, a", "swap", ctx)]


class _LyingRule(Rule):
    """Reports a rewrite without changing anything."""

    name = "lying"
    title = "Lying"
    description = "test"
    category = "test"

    def apply(self, expression, ctx):
        return expression, [self.rewrite("a", "a", "nothing", ctx)]


def statements(sql: str, dialect: str = "duckdb"):
    parsed = parse_sql(sql, dialect)
    assert parsed.ok, parsed.errors
    return parsed.statements


# --------------------------------------------------------------------- the engine
def test_full_pipeline_on_messy_query():
    result = optimize_query(MESSY, "postgres")
    assert isinstance(result, OptimizationResult) and result.changed
    rules = [rw.rule for rw in result.rewrites]
    assert {"explicit-join", "sargable-date", "not-in-to-not-exists", "simplify", "or-to-in", "null-comparison"} <= set(rules)
    out = result.optimized_sql
    assert "JOIN customers AS c" in out and "NOT EXISTS" in out and "IN ('a', 'b', 'c')" in out
    assert "1 = 1" not in out and "IS NULL" in out
    assert result.passes >= 2 and not result.errors
    assert "need review" in result.summary() and not result.safe_only
    assert set(result.by_rule()) == set(rules)
    for rw in result.rewrites:
        assert isinstance(rw, Rewrite) and rw.before and rw.after and rw.description and rw.title


def test_optimization_is_idempotent():
    first = optimize_query(MESSY, "postgres")
    second = optimize_query(first.optimized_sql, "postgres")
    assert not second.changed, [rw.rule for rw in second.rewrites]
    assert second.optimized_sql == first.optimized_sql  # unchanged queries are returned verbatim


def test_result_statements_are_new_objects_and_parse_back():
    stmts = statements(MESSY, "duckdb")
    before = stmts[0].sql()
    result = optimize_query(stmts, "duckdb")
    assert stmts[0].sql() == before  # input is never mutated
    assert result.statements[0] is not stmts[0]
    for s in sqlglot.parse(result.optimized_sql, read="duckdb"):
        assert s is not None


def test_unchanged_query_reports_no_rewrites():
    result = optimize_query("SELECT a FROM t WHERE a = 1 ORDER BY a", "postgres")
    assert not result.changed and result.summary() == "No rewrites applied." and result.passes == 1
    assert result.optimized_sql.startswith("SELECT")


def test_invalid_sql_raises():
    with pytest.raises(ValueError):
        optimize_query("SELECT FROM WHERE", "postgres")


def test_multiple_statements_are_optimized_independently():
    result = optimize_query("SELECT a FROM t WHERE a = 1 OR a = 2; DELETE FROM u WHERE b = NULL", "duckdb")
    assert len(result.statements) == 2
    assert [rw.rule for rw in result.rewrites] == ["or-to-in", "null-comparison"]
    assert result.optimized_sql.count(";") == 2


def test_broken_rewrites_are_discarded_and_reported():
    result = Optimizer([_BrokenRule()]).optimize(statements("SELECT a FROM t WHERE a = 1"), "duckdb")
    assert not result.changed
    assert result.errors and "broken" in result.errors[0] and "unknown tables" in result.errors[0]


def test_raising_rule_does_not_abort_the_run():
    rules = [_RaisingRule(), rule_by_name("or-to-in")]
    result = Optimizer(rules).optimize(statements("SELECT a FROM t WHERE a = 1 OR a = 2"), "duckdb")
    assert result.changed and [rw.rule for rw in result.rewrites] == ["or-to-in"]
    assert any("raising: RuntimeError: boom" in e for e in result.errors)


def test_flip_flop_rule_terminates():
    result = Optimizer([_FlipFlopRule()], max_passes=10).optimize(statements("SELECT a, b FROM t"), "duckdb")
    assert len(result.rewrites) == 1  # second application would undo the first: discarded
    assert any("undoes an earlier rewrite" in e for e in result.errors)
    assert result.passes <= 3


def test_rewrites_without_visible_change_are_ignored():
    result = Optimizer([_LyingRule()]).optimize(statements("SELECT a FROM t"), "duckdb")
    assert not result.changed and not result.errors


def test_disabled_rules_are_skipped():
    result = optimize_query("SELECT a FROM t WHERE a = 1 OR a = 2", "duckdb", disabled=["or-to-in"])
    assert not result.changed and result.skipped["or-to-in"] == "disabled"


def test_include_review_false_only_applies_safe_rules():
    result = optimize_query(MESSY, "postgres", include_review=False)
    assert result.changed and result.safe_only
    applied = {rw.rule for rw in result.rewrites}
    assert "not-in-to-not-exists" not in applied and "null-comparison" not in applied
    assert "explicit-join" in applied and "or-to-in" in applied
    assert all("review" in reason for name, reason in result.skipped.items()
               if name in ("not-in-to-not-exists", "null-comparison", "union-all"))


def test_schema_enables_scope_aware_passes():
    sql = "SELECT s.id FROM (SELECT id, amount FROM orders WHERE amount > 5) s WHERE s.id > 10"
    without = optimize_query(sql, "duckdb")
    assert not without.qualified and "merge-subqueries" in without.skipped
    with_schema = optimize_query(sql, "duckdb", schema=SCHEMA)
    assert with_schema.qualified and "merge-subqueries" in {rw.rule for rw in with_schema.rewrites}
    assert "(SELECT" not in with_schema.optimized_sql


def test_qualification_noise_is_stripped_from_output():
    result = optimize_query("SELECT id, 1, (SELECT max(amount) FROM orders) FROM orders WHERE id = 1 OR id = 2",
                            "duckdb", schema=SCHEMA)
    out = result.optimized_sql
    assert result.qualified and "AS id" not in out and 'AS "1"' not in out and "_col_" not in out
    assert "orders AS orders" not in out


def test_max_passes_is_respected():
    result = optimize_query(MESSY, "postgres", max_passes=1)
    assert result.passes == 1 and result.changed


def test_default_rule_order_runs_qualify_first():
    names = [r.name for r in default_rules()]
    assert names[0] == "qualify" and "unnest-subqueries" not in names
    assert names.index("simplify") < names.index("pushdown-predicates")


# ------------------------------------------------------------------- utilities
def test_canonical_sql_ignores_commutative_order():
    a = sqlglot.parse_one("SELECT * FROM t JOIN u ON t.x = u.x WHERE b = 2 AND a = 1 OR c = 3")
    b = sqlglot.parse_one("SELECT * FROM t INNER JOIN u ON u.x = t.x WHERE c = 3 OR (1 = a AND b = 2)")
    assert canonical_sql(a) == canonical_sql(b)
    c = sqlglot.parse_one("SELECT * FROM t JOIN u ON t.x = u.x WHERE b = 2 AND a = 1 OR c = 4")
    assert canonical_sql(a) != canonical_sql(c)
    assert a.sql() == "SELECT * FROM t JOIN u ON t.x = u.x WHERE b = 2 AND a = 1 OR c = 3"  # not mutated


def test_invalid_references_and_validate():
    ok = sqlglot.parse_one("SELECT (SELECT max(q.v) FROM q WHERE q.i = a.i) FROM a JOIN b ON a.x = b.x")
    assert invalid_references(ok) == [] and validate(ok, "postgres") is None
    bad = sqlglot.parse_one("SELECT o.id FROM orders JOIN customers AS c ON o.customer_id = c.id")
    assert invalid_references(bad) == ["o.id", "o.customer_id"]
    assert "unknown tables" in validate(bad, "postgres")


def test_strip_qualification_noise():
    e = sqlglot.parse_one('SELECT o.id AS id, 1 AS "1", o.x AS y, (SELECT 1) AS _col_1, COUNT(*) AS _col_2 '
                          "FROM orders AS orders JOIN orders AS o ON o.id = orders.id")
    out = strip_qualification_noise(e).sql()
    assert out == ("SELECT o.id, 1, o.x AS y, (SELECT 1), COUNT(*) "
                   "FROM orders JOIN orders AS o ON o.id = orders.id")
    inner = sqlglot.parse_one("SELECT s.a FROM (SELECT t.a AS a, 1 AS _col_0 FROM t AS t) AS s")
    assert strip_qualification_noise(inner).sql() == "SELECT s.a FROM (SELECT t.a, 1 AS _col_0 FROM t) AS s"


def test_rule_context_defaults():
    ctx = RuleContext("postgres")
    assert ctx.schema is None and not ctx.qualified and ctx.include_review and ctx.extra == {}
    assert Safety("safe") is Safety.SAFE


# ---------------------------------------------------------------------- schema
def test_parse_schema_basic_and_types():
    hints = parse_schema("""
        # comment
        orders: id int, customer_id int, amount decimal(10, 2), status varchar(20)
        customers: id, name   # types optional
    """)
    assert not hints.errors and not hints.empty
    assert hints.tables["orders"] == {"id": "INT", "customer_id": "INT", "amount": "DECIMAL(10, 2)",
                                      "status": "VARCHAR(20)"}
    assert hints.tables["customers"] == {"id": "UNKNOWN", "name": "UNKNOWN"}
    assert hints.mapping() == {"orders": hints.tables["orders"], "customers": hints.tables["customers"]}
    assert hints.column_names("ORDERS") == ["id", "customer_id", "amount", "status"]
    assert hints.column_names("nope") == []


def test_parse_schema_qualified_tables_and_errors():
    hints = parse_schema("sales.orders: id int\nsales.customers: id int, name text")
    assert hints.mapping() == {"sales": {"orders": {"id": "INT"}, "customers": {"id": "INT", "name": "TEXT"}}}
    assert hints.column_names("orders") == ["id"]
    mixed = parse_schema("orders: id int\nsales.customers: id int")
    assert mixed.empty and "same qualification depth" in mixed.errors[0]
    bad = parse_schema("no colon here\norders:\n1bad: x int\norders: 1x int")
    assert bad.empty and [e.split(":")[0] for e in bad.errors] == ["line 1", "line 2", "line 3", "line 4", "line 4"]
    assert "invalid table name" in bad.errors[2] and "invalid column definition" in bad.errors[3]
    assert parse_schema("").empty and parse_schema("").mapping() is None


def test_schema_hints_drive_the_optimizer():
    hints = parse_schema("orders: id int, customer_id int, status text\ncustomers: id int, name text")
    result = optimize_query("SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.status = 'a'",
                            "postgres", schema=hints.mapping())
    assert result.qualified and result.rewrites[0].rule == "qualify"
    assert "o.id, o.customer_id, o.status, c.id, c.name" in " ".join(result.optimized_sql.split())


# ------------------------------------------------------------ analyzer integration
def test_analyze_query_includes_ast_and_optimization():
    a = analyze_query(MESSY, Engine.POSTGRES)
    assert a.syntax_ok and len(a.statements) == 1 and len(a.ast) == 1
    assert a.ast[0].kind == "Select" and a.ast[0].expression is a.statements[0]
    assert a.optimization is not None and a.optimization.changed
    resolved = {f.rule for f in a.resolved_by_optimization}
    assert {"implicit-join", "non-sargable-predicate", "not-in-subquery", "null-comparison"} <= resolved
    assert all(f.rule != "implicit-join" for f in a.optimized_findings)


def test_analyze_query_optimization_can_be_disabled_and_configured():
    a = analyze_query(MESSY, Engine.POSTGRES, optimize=False)
    assert a.optimization is None and a.resolved_by_optimization == [] and a.ast
    b = analyze_query(MESSY, Engine.POSTGRES, include_review=False, disabled_rules={"or-to-in"})
    assert b.optimization.safe_only and "or-to-in" in b.optimization.skipped
    c = analyze_query(MESSY, Engine.POSTGRES, schema=SCHEMA)
    assert c.optimization.qualified


def test_analyze_query_with_syntax_error_has_no_ast_or_optimization():
    a = analyze_query("SELECT FROM WHERE", Engine.DUCKDB)
    assert not a.syntax_ok and a.ast == [] and a.optimization is None and a.optimized_findings == []


def test_optimized_query_has_no_more_findings_than_original():
    original = analyze_query(MESSY, Engine.POSTGRES)
    rewritten = analyze_query(original.optimization.optimized_sql, Engine.POSTGRES)
    assert len(rewritten.findings) < len(original.findings)
    cmp = compare_queries(original, rewritten)
    assert {f.rule for f in cmp.resolved} >= {"implicit-join", "not-in-subquery"}
    assert not any(f.rule == "cartesian-join" for f in cmp.introduced)


@pytest.mark.parametrize("engine", list(Engine))
def test_analyze_query_in_every_engine(engine):
    a = analyze_query("SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id AND o.x = 1 OR o.x = 2", engine)
    assert a.syntax_ok and a.optimization is not None and a.optimization.changed
    assert a.optimization.dialect == engine.value if engine is not Engine.POSTGRES else "postgres"
