import pytest

from conftest import read
from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.dialects import Engine, explain_statement
from sqlanalyzer.plans import PlanParseError, parse_plan
from sqlanalyzer.plans.analysis import analyze_plan
from sqlanalyzer.plans.diff import ADDED, REMOVED, diff_plans
from sqlanalyzer.plans.graph import build_graph, graphviz_available, heat_map, render_pdf, render_png, render_svg


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
    # no EXPLAIN was run, so the offline plans are compared instead
    assert cmp.offline_plans and cmp.plan_diff is not None and not cmp.plan_diff.identical
    assert any("Cross Join" in (e.old.op if e.old else "") for e in cmp.plan_diff.entries)


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


def test_graph_nodes_and_edges_have_stable_ids():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    src = build_graph(plan).source
    for node in plan.nodes():
        assert f"id=n{node.id} " in src
    for node in plan.nodes():
        for child in node.children:
            assert f"e{child.id}_{node.id}" in src


def test_heat_map_shares_sum_to_one_and_prefers_measured_time():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    heat = heat_map(plan)
    assert plan.analyzed  # this fixture has actual timings
    assert heat and abs(sum(heat.values()) - 1.0) < 1e-9
    assert all(0.0 <= v <= 1.0 for v in heat.values())


def test_heat_map_empty_without_any_cost_or_time():
    from sqlanalyzer.plans.model import OTHER, Plan, PlanNode

    plan = Plan(engine=Engine.POSTGRES, root=PlanNode(id=0, op="Result", kind=OTHER))
    assert heat_map(plan) == {}


def test_heat_color_is_monotonic_and_clamped():
    from sqlanalyzer.plans.graph import heat_color

    lo, mid, hi = heat_color(0.0), heat_color(0.5), heat_color(1.0)
    assert lo != mid != hi
    assert heat_color(-5) == lo and heat_color(5) == hi


def test_build_graph_with_heat_fills_nodes_with_the_ramp_colour():
    from sqlanalyzer.plans.graph import heat_color

    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    heat = {plan.root.id: 1.0}
    src = build_graph(plan, heat=heat).source
    assert heat_color(1.0) in src
    assert "heat 100%" in src


def test_build_graph_collapsed_subtree_hides_descendants():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    collapsed = {plan.root.id}
    src = build_graph(plan, collapsed=collapsed).source
    for node in plan.nodes():
        if node.id != plan.root.id:
            assert f"id=n{node.id} " not in src
    assert "collapsed" in src


def test_build_graph_glyphs_do_not_rely_on_colour_alone():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    src = build_graph(plan, severities={plan.root.id: "error"}).source
    assert "\u26d4" in src  # the error glyph, not just a red border
    from sqlanalyzer.plans.diff import ADDED

    src2 = build_graph(plan, status={plan.root.id: ADDED}).source
    assert "+ " in src2


@needs_dot
def test_render_png_and_pdf():
    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    png = render_png(plan)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    pdf = render_pdf(plan)
    assert pdf[:4] == b"%PDF"


_SVG = b"""<?xml version="1.0"?>
<svg width="100pt" height="100pt" viewBox="0.00 0.00 100.00 100.00" xmlns="http://www.w3.org/2000/svg">
<g id="graph0" class="graph" transform="scale(1 1) rotate(0) translate(4 96)">
<g id="n0" class="node"><title>n0</title>
<path d="M50,-90C50,-90 10,-90 10,-90 4,-90 0,-86 0,-80 0,-80 0,-60 0,-60 0,-54 4,-50 10,-50 50,-50 56,-50 60,-54 60,-60 60,-80 60,-86 56,-90 50,-90"/>
<text x="5" y="-70">label</text></g>
<g id="n1" class="node"><title>n1</title><polygon points="20,-10 80,-10 80,-40 20,-40 20,-10"/></g>
<g id="n2" class="node"><title>n2</title><ellipse cx="50" cy="-25" rx="10" ry="5"/></g>
<g id="e1_0" class="edge"><title>n1-&gt;n0</title><path d="M30,-40C30,-45 30,-47 30,-50"/></g>
<g id="a_x"><text x="1" y="1">not a node group</text></g>
</g>
</svg>"""


def test_element_bounds_reads_node_geometry_with_the_root_transform():
    from sqlanalyzer.plans.graph import element_bounds

    boxes = element_bounds(_SVG)
    assert set(boxes) == {"n0", "n1", "n2", "e1_0"}  # edges too; untyped groups and text are ignored
    # polygon: x 20..80 -> +4 translate; y -40..-10 -> +96 translate
    assert boxes["n1"] == pytest.approx((24.0, 56.0, 60.0, 30.0))
    # rounded path: min/max over every coordinate in the path
    assert boxes["n0"] == pytest.approx((4.0, 6.0, 60.0, 40.0))
    # ellipse: centre +- radii
    assert boxes["n2"] == pytest.approx((44.0, 66.0, 20.0, 10.0))


def test_element_bounds_honours_scale_and_viewbox_origin():
    from sqlanalyzer.plans.graph import element_bounds

    svg = _SVG.replace(b'viewBox="0.00 0.00 100.00 100.00"', b'viewBox="10 20 100 100"') \
              .replace(b"scale(1 1)", b"scale(2 0.5)")
    boxes = element_bounds(svg)
    x, y, w, h = boxes["n1"]
    assert (x, w) == pytest.approx(((20 + 4) * 2 - 10, 60 * 2))
    assert (y, h) == pytest.approx(((-40 + 96) * 0.5 - 20, 30 * 0.5))


def test_element_bounds_is_empty_for_garbage():
    from sqlanalyzer.plans.graph import element_bounds

    assert element_bounds(b"not xml") == {}
    assert element_bounds(b"<svg xmlns='http://www.w3.org/2000/svg'/>") == {}


@needs_dot
def test_element_bounds_match_every_rendered_node():
    from sqlanalyzer.plans.graph import element_bounds

    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    boxes = element_bounds(render_svg(plan))
    assert {f"n{n.id}" for n in plan.nodes()} <= set(boxes)
    rects = [boxes[f"n{n.id}"] for n in plan.nodes()]
    for i, (x, y, w, h) in enumerate(rects):  # graphviz never overlaps nodes
        for x2, y2, w2, h2 in rects[i + 1:]:
            assert x + w <= x2 or x2 + w2 <= x or y + h <= y2 or y2 + h2 <= y
