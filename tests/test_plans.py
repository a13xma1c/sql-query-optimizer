import pytest

from conftest import read
from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.dialects import Engine, explain_statement
from sqlanalyzer.plans import PlanParseError, parse_plan
from sqlanalyzer.plans.analysis import analyze_plan
from sqlanalyzer.plans.diff import ADDED, REMOVED, diff_plans
from sqlanalyzer.plans.graph import build_graph, graphviz_available, render_svg


def rules(plan) -> set[str]:
    return {f.rule for f in analyze_plan(plan)}


def test_postgres_analyzed_plan():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    assert plan.analyzed and plan.total_time_ms == 950.0
    root = plan.root
    assert root.kind == "join" and root.join_algo == "hash" and root.join_type == "INNER"
    scan = root.children[0]
    assert scan.relation == "public.orders" and scan.filter and scan.actual_rows == 400000
    assert root.self_time_ms == pytest.approx(950 - 600 - 20)
    r = rules(plan)
    assert {"scan-discards-rows", "hash-spill", "row-misestimate", "function-in-scan-filter"} <= r


def test_postgres_nested_loop_cartesian():
    plan = parse_plan(Engine.POSTGRES, read("postgres_nested_loop.json"))
    assert "cartesian-product" in rules(plan)


def test_postgres_partition_append():
    parts = [{"Node Type": "Seq Scan", "Relation Name": f"ev_p{i}", "Total Cost": 10, "Plan Rows": 10}
             for i in range(12)]
    doc = [{"Plan": {"Node Type": "Append", "Total Cost": 120, "Plan Rows": 120, "Subplans Removed": 0, "Plans": parts}}]
    assert "no-partition-pruning" in rules(parse_plan(Engine.POSTGRES, __import__("json").dumps(doc)))
    doc[0]["Plan"]["Subplans Removed"] = 10
    doc[0]["Plan"]["Plans"] = parts[:2]
    assert "no-partition-pruning" not in rules(parse_plan(Engine.POSTGRES, __import__("json").dumps(doc)))


def test_snowflake_plan():
    plan = parse_plan(Engine.SNOWFLAKE, read("snowflake_plan.json"))
    ops = [n.op for n in plan.nodes()]
    assert ops[0] == "Result" and ops.count("TableScan") == 2
    join = next(n for n in plan.nodes() if n.kind == "join")
    assert join.join_type == "INNER" and "O.CUSTOMER_ID" in (join.condition or "")
    assert {n.relation for n in plan.nodes() if n.relation} == {"DB.S.ORDERS", "DB.S.CUSTOMERS"}
    assert "poor-partition-pruning" in rules(plan)


def test_trino_plan_fragments_are_linked():
    plan = parse_plan(Engine.TRINO, read("trino_plan.txt"))
    names = [n.op for n in plan.nodes()]
    assert names[0] == "Output" and "InnerJoin" in names and names.count("RemoteSource") == 2
    join = next(n for n in plan.nodes() if n.op == "InnerJoin")
    assert join.join_type == "INNER" and join.condition == "(custkey = custkey_0)"
    scans = {n.relation: n for n in plan.nodes() if n.kind == "scan"}
    assert set(scans) == {"hive.sales.orders", "hive.sales.customer"}
    assert scans["hive.sales.orders"].est_rows == 15_000_000
    assert "no-partition-pruning" in rules(plan)  # dt partition key is unconstrained
    assert any(n.op == "TableScan" for n in join.children[1].walk())  # fragment 2 under RemoteSource


def test_invalid_plans_raise():
    for engine, text in [(Engine.POSTGRES, "not json"), (Engine.POSTGRES, "[{}]"), (Engine.SNOWFLAKE, "{}"),
                         (Engine.TRINO, "hello"), (Engine.DUCKDB, "[]")]:
        with pytest.raises(PlanParseError):
            parse_plan(engine, text)


def test_explain_statements():
    assert explain_statement(Engine.POSTGRES, "select 1;").startswith("EXPLAIN (FORMAT JSON) select 1")
    assert "ANALYZE" in explain_statement(Engine.POSTGRES, "select 1", True)
    assert explain_statement(Engine.TRINO, "select 1") == "EXPLAIN (TYPE DISTRIBUTED) select 1"
    assert explain_statement(Engine.SNOWFLAKE, "select 1", True) == "EXPLAIN USING JSON select 1"


def test_plan_diff_detects_changes():
    old = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    import json
    doc = json.loads(read("postgres_plan.json"))
    doc[0]["Plan"]["Node Type"] = "Merge Join"
    doc[0]["Plan"]["Plans"][1] = {"Node Type": "Index Scan", "Relation Name": "customers", "Schema": "public",
                                  "Total Cost": 100, "Plan Rows": 10000}
    doc[0]["Plan"]["Total Cost"] = 40000.0
    new = parse_plan(Engine.POSTGRES, json.dumps(doc))
    d = diff_plans(old, new)
    statuses = [e.status for e in d.entries]
    assert ADDED in statuses and REMOVED in statuses and not d.identical
    assert any("Cost" in s or "cost" in s for s in d.summary)
    assert diff_plans(old, old).identical


def test_compare_queries_reports_resolved_findings():
    a = analyze_query("SELECT * FROM t, u WHERE t.x = 1", Engine.POSTGRES)
    b = analyze_query("SELECT t.x FROM t JOIN u ON t.id = u.id WHERE t.x = 1", Engine.POSTGRES)
    cmp = compare_queries(a, b)
    assert {f.rule for f in cmp.resolved} >= {"select-star", "cartesian-join"}
    assert cmp.plan_diff is None


def test_plan_parse_failure_becomes_finding():
    res = analyze_query("SELECT 1", Engine.POSTGRES, plan_text="garbage")
    assert res.plan is None and any(f.rule == "plan-parse-error" for f in res.findings)


needs_dot = pytest.mark.skipif(not graphviz_available(), reason="Graphviz 'dot' not installed")


def test_graph_source_contains_all_nodes():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    src = build_graph(plan, severities={2: "warning"}).source
    assert src.count("->") == len(plan.nodes()) - 1 and "Seq Scan" in src and "#f59e0b" in src


@needs_dot
def test_render_svg():
    for engine, name in [(Engine.POSTGRES, "postgres_plan.json"), (Engine.SNOWFLAKE, "snowflake_plan.json"),
                         (Engine.TRINO, "trino_plan.txt")]:
        svg = render_svg(parse_plan(engine, read(name)))
        assert svg.startswith(b"<?xml") and b"<svg" in svg
