"""The logical (connection-free) plan and the plan explanation."""

import pytest

from conftest import read
from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import Engine
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.plans import parse_plan
from sqlanalyzer.plans.analysis import analyze_plan
from sqlanalyzer.plans.explain import explain_plan, explain_text
from sqlanalyzer.plans.graph import build_graph
from sqlanalyzer.plans.logical import build_logical_plan


def plan_of(sql: str, engine: Engine = Engine.DUCKDB):
    statements = parse_sql(sql, engine.value if engine is not Engine.POSTGRES else "postgres").statements
    assert statements, sql
    return build_logical_plan(statements[0], engine)


def ops(plan) -> list[str]:
    return [n.op for n in plan.nodes()]


# --------------------------------------------------------------------- plan shape
def test_scan_filter_join_aggregate_sort_limit_order():
    plan = plan_of("SELECT o.id, count(*) FROM orders o, customers c "
                   "WHERE o.customer_id = c.id AND o.status = 'a' AND c.region = 'EU' AND o.amount > c.credit "
                   "GROUP BY o.id HAVING count(*) > 2 ORDER BY 1 LIMIT 10")
    assert plan.logical and not plan.analyzed
    assert plan.source.startswith("derived from the SQL")
    assert ops(plan) == ["Limit", "Top-N Sort", "Filter (HAVING)", "Aggregate", "Filter", "Hash Join", "Scan", "Scan"]
    scans = {n.relation: n for n in plan.nodes() if n.kind == "scan"}
    assert scans["orders"].filter == "o.status = 'a'"  # single-table predicates are pushed to the scan
    assert scans["customers"].filter == "c.region = 'EU'"
    join = next(n for n in plan.nodes() if n.kind == "join")
    assert join.join_algo == "hash" and join.condition == "o.customer_id = c.id" and join.attrs["implicit"]
    residual = next(n for n in plan.nodes() if n.op == "Filter")
    assert residual.filter == "o.amount > c.credit"  # multi-table predicate stays above the join
    assert all(n.est_rows is None and n.cost is None for n in plan.nodes())


def test_join_algorithms_follow_the_predicate():
    assert next(n for n in plan_of("SELECT 1 FROM a JOIN b ON a.x = b.x").nodes() if n.kind == "join").join_algo == "hash"
    nl = next(n for n in plan_of("SELECT 1 FROM a JOIN b ON a.x > b.x").nodes() if n.kind == "join")
    assert nl.join_algo == "nested_loop" and nl.op == "Nested Loop Join"
    cross = next(n for n in plan_of("SELECT 1 FROM a, b").nodes() if n.kind == "join")
    assert cross.join_type == "CROSS" and "Cartesian" in cross.op
    left = next(n for n in plan_of("SELECT 1 FROM a LEFT JOIN b ON a.x = b.x").nodes() if n.kind == "join")
    assert left.join_type == "LEFT"
    using = next(n for n in plan_of("SELECT 1 FROM a JOIN b USING (x)").nodes() if n.kind == "join")
    assert using.condition == "USING (x)" and using.join_algo == "hash"


def test_ctes_derived_tables_and_subqueries():
    plan = plan_of("WITH r AS (SELECT id FROM refunds WHERE amt > 0) "
                   "SELECT s.id FROM (SELECT id FROM orders) s JOIN r ON r.id = s.id")
    assert "CTE r" in ops(plan) and "Derived table" in ops(plan)
    cte = next(n for n in plan.nodes() if n.op == "CTE r")
    assert cte.children and cte.children[0].relation == "refunds"
    # a recursive CTE must not recurse forever
    recursive = plan_of("WITH RECURSIVE r AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM r WHERE n < 5) "
                        "SELECT n FROM r")
    assert "CTE r" in ops(recursive) and len(recursive.nodes()) < 20


def test_window_distinct_and_set_operations():
    plan = plan_of("SELECT x, row_number() OVER (PARTITION BY z ORDER BY y) FROM t")
    window = next(n for n in plan.nodes() if n.op == "Window")
    assert "ROW_NUMBER()" in window.attrs["functions"] and "partition by z" in window.attrs["partitioning"]
    assert "Aggregate" not in " ".join(ops(plan))  # windowed aggregates are not a GROUP BY
    assert ops(plan_of("SELECT DISTINCT a FROM t")) == ["Distinct", "Scan"]
    union = plan_of("SELECT a FROM t UNION SELECT b FROM u ORDER BY 1")
    assert union.root.op == "Sort" and union.root.children[0].op == "Union"
    assert union.root.children[0].attrs["duplicates"] == "removed"
    assert plan_of("SELECT a FROM t UNION ALL SELECT b FROM u").root.op == "Union All"


def test_dml_and_odd_statements():
    assert ops(plan_of("DELETE FROM t WHERE x = 1")) == ["Delete", "Scan"]
    assert ops(plan_of("UPDATE t SET a = 1 WHERE x = 2")) == ["Update", "Scan"]
    assert ops(plan_of("INSERT INTO t SELECT a FROM u"))[0] == "Insert"
    assert ops(plan_of("SELECT 1")) == ["Values"]
    assert plan_of("CREATE TABLE x AS SELECT a FROM t").root.op.startswith("Create")


@pytest.mark.parametrize("engine", list(Engine))
def test_logical_plan_for_every_engine(engine):
    plan = plan_of("SELECT a.id FROM a JOIN b ON a.id = b.id WHERE a.x = 1 ORDER BY 1", engine)
    assert plan.engine is engine and len(plan.nodes()) == 4
    assert build_graph(plan).source.count("->") == 3


def test_plan_ids_are_unique_and_graph_renders():
    plan = plan_of("SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id")
    ids = [n.id for n in plan.nodes()]
    assert sorted(ids) == list(range(len(ids)))
    assert "Hash Join" in build_graph(plan).source


# -------------------------------------------------------------------- explanation
def test_explanation_of_offline_plan():
    plan = plan_of("SELECT o.id, count(*) FROM orders o JOIN customers c ON o.customer_id = c.id "
                   "WHERE DATE(o.ts) = '2024-01-01' GROUP BY o.id ORDER BY 1")
    explanation = explain_plan(plan)
    assert "no database was contacted" in explanation.headline
    assert [s.order for s in explanation.steps] == list(range(1, len(plan.nodes()) + 1))
    assert explanation.steps[0].title.startswith("Scan on")  # children are explained first
    assert explanation.steps[-1].node_id == plan.root.id
    texts = {s.title: s for s in explanation.steps}
    scan = next(s for s in explanation.steps if "orders" in s.title)
    assert "keeping only rows where" in scan.detail
    assert any("function/cast" in n for n in scan.notes)
    customers = next(s for s in explanation.steps if "customers" in s.title)
    assert any("whole table is read" in n for n in customers.notes)
    join = texts["Hash Join"]
    assert "hash table" in join.detail and "o.customer_id = c.id" in join.detail
    assert "Groups the rows by o.id" in texts["Aggregate"].detail
    assert all(s.metrics == "" for s in explanation.steps)  # nothing is measured offline
    assert any("Tables read" in s for s in explanation.summary)
    assert any("No cardinalities" in s for s in explanation.summary)
    assert explanation.step_for(plan.root.id) is explanation.steps[-1]
    assert explanation.step_for(999) is None


def test_explanation_notes_for_cartesian_and_limit():
    explanation = explain_plan(plan_of("SELECT a.x FROM a, b LIMIT 10"))
    join = next(s for s in explanation.steps if "Cartesian" in s.title)
    assert "every row of the left input" in join.detail
    assert any("Cartesian product" in n for n in join.notes)
    limit = next(s for s in explanation.steps if s.title == "Limit")
    assert any("arbitrary subset" in n for n in limit.notes)


def test_explanation_text_rendering():
    text = explain_text(plan_of("SELECT a FROM t WHERE b = 1 ORDER BY a"))
    assert text.startswith("Expected execution for DuckDB")
    assert "1. Scan on t" in text and "2. Sort" in text and "Summary:" in text


def test_explanation_of_a_real_plan_includes_metrics_and_findings():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    explanation = explain_plan(plan, analyze_plan(plan))
    assert "measured row counts" in explanation.headline
    scan = next(s for s in explanation.steps if "orders" in s.title)
    assert "400,000 rows actual" in scan.metrics and "ms in this operator" in scan.metrics
    assert any("discarded 92%" in n for n in scan.notes)  # plan findings become notes
    assert any("Measured total time" in s for s in explanation.summary)


def test_explanation_of_estimated_plan_and_exchanges():
    plan = parse_plan(Engine.TRINO, read("trino_plan.txt"))
    explanation = explain_plan(plan)
    assert "estimates" in explanation.headline
    assert any("rows estimated" in s.metrics for s in explanation.steps)
    assert any("Moves rows between workers" in s.detail for s in explanation.steps)
    assert any("shuffles data" in s for s in explanation.summary)


# -------------------------------------------------------------- analyzer wiring
def test_analysis_always_has_a_plan_and_explanation_without_a_database():
    a = analyze_query("SELECT * FROM orders o, customers c WHERE o.customer_id = c.id", Engine.DUCKDB, optimize=False)
    assert a.plan is None  # no EXPLAIN was run
    assert a.logical_plan is not None and a.effective_plan is a.logical_plan
    assert a.explanation is not None and len(a.explanation.steps) == len(a.logical_plan.nodes())
    assert "no database was contacted" in a.explanation.headline


def test_real_plan_takes_precedence_over_the_offline_one():
    a = analyze_query("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id", Engine.POSTGRES,
                      plan_text=read("postgres_plan.json"), optimize=False)
    assert a.plan is not None and a.logical_plan is not None
    assert a.effective_plan is a.plan
    assert "measured" in a.explanation.headline
    assert a.explanation.steps[0].metrics


def test_broken_sql_has_no_logical_plan():
    a = analyze_query("SELECT FROM WHERE", Engine.DUCKDB, optimize=False)
    assert a.logical_plan is None and a.effective_plan is None and a.explanation is None


def test_exotic_sql_does_not_break_the_logical_plan():
    for sql in ("SELECT * FROM t PIVOT (sum(x) FOR y IN ('a'))",
                "SELECT * FROM UNNEST([1, 2]) AS t(x)",
                "SELECT * FROM (VALUES (1), (2)) AS v(a)",
                "MERGE INTO t USING u ON t.id = u.id WHEN MATCHED THEN UPDATE SET a = u.a",
                "SELECT t.a FROM t CROSS JOIN LATERAL (SELECT b FROM v WHERE v.a = t.a) l"):
        a = analyze_query(sql, Engine.DUCKDB, optimize=False)
        if a.syntax_ok:
            assert a.logical_plan is not None and a.explanation is not None, sql
            assert a.explanation.text()


def test_compare_falls_back_to_offline_plans():
    from sqlanalyzer.analyzer import compare_queries

    a = analyze_query("SELECT a.x FROM a, b WHERE a.k = 1", Engine.DUCKDB, optimize=False)
    b = analyze_query("SELECT a.x FROM a JOIN b ON a.id = b.id WHERE a.k = 1", Engine.DUCKDB, optimize=False)
    cmp = compare_queries(a, b)
    assert cmp.offline_plans and cmp.plan_diff is not None and not cmp.plan_diff.identical
    assert compare_queries(a, a).plan_diff.identical
    broken = analyze_query("SELECT FROM", Engine.DUCKDB, optimize=False)
    assert compare_queries(a, broken).plan_diff is None and not compare_queries(a, broken).offline_plans
