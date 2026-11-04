"""PostgreSQL end-to-end stories on a real server: the rewrites change the real plans for the better."""

import pytest

from sqlanalyzer.analyzer import analyze_query, compare_queries
from sqlanalyzer.db import fetch_plan_text
from sqlanalyzer.dialects import Engine
from sqlanalyzer.optimizer import optimize_query
from sqlanalyzer.plans.explain import explain_plan

from .conftest import MESSY_SQL
from .test_engines_e2e import rows

pytestmark = pytest.mark.e2e

PARTITIONED = "SELECT count(*) FROM events WHERE date_trunc('month', dt) = DATE '2024-03-01'"


@pytest.fixture
def pg(pg_server):
    if pg_server is None:
        pytest.skip("no PostgreSQL available")
    from .conftest import EngineEnv

    return EngineEnv(Engine.POSTGRES, pg_server, True)


def analyzed(pg, sql: str, analyze: bool):
    return analyze_query(sql, Engine.POSTGRES, plan_text=fetch_plan_text(pg.cfg, sql, analyze))


def test_explain_analyze_with_buffers_is_measured(pg):
    a = analyzed(pg, MESSY_SQL, analyze=True)
    plan = a.plan
    assert plan.analyzed and plan.total_time_ms and plan.total_time_ms > 0
    assert plan.total_cost and plan.total_cost > 0
    scan = next(n for n in plan.nodes() if n.relation == "orders")
    assert scan.actual_rows is not None and scan.attrs.get("Rows Removed by Filter")
    assert "Shared Hit Blocks" in scan.attrs or "Shared Read Blocks" in scan.attrs  # BUFFERS was requested
    step = a.explanation.step_for(scan.id)
    assert "rows actual" in step.metrics and "ms in this operator" in step.metrics


def test_rewrite_turns_a_sequential_scan_into_an_index_scan(pg):
    before = analyzed(pg, MESSY_SQL, analyze=True)
    seq = next(n for n in before.plan.nodes() if n.relation == "orders")
    assert seq.op == "Seq Scan" and seq.filter and "ts" in seq.filter
    rules_before = {f.rule for f in before.findings if f.node_id is not None}
    assert {"scan-discards-rows", "function-in-scan-filter"} <= rules_before

    rewritten = optimize_query(MESSY_SQL, "postgres").optimized_sql
    after = analyzed(pg, rewritten, analyze=True)
    orders = [n for n in after.plan.nodes() if n.relation == "orders"]
    assert orders and all("Seq Scan" != n.op for n in orders)
    assert any("Index" in n.op for n in after.plan.nodes())  # Bitmap/Index scan on orders_ts_idx
    assert not {f.rule for f in after.findings if f.node_id is not None} & {"scan-discards-rows",
                                                                             "function-in-scan-filter"}
    assert after.plan.total_time_ms < before.plan.total_time_ms
    assert after.plan.total_cost < before.plan.total_cost
    assert rows(pg, rewritten) == rows(pg, MESSY_SQL)

    cmp = compare_queries(before, after)
    assert cmp.plan_diff is not None and not cmp.plan_diff.identical and not cmp.offline_plans
    assert any("Execution time" in line or "cost" in line.lower() for line in cmp.plan_diff.summary)
    assert {f.rule for f in cmp.resolved} >= {"scan-discards-rows", "non-sargable-predicate", "implicit-join"}


def test_rewrite_enables_partition_pruning(pg):
    before = analyzed(pg, PARTITIONED, analyze=False)
    append = next(n for n in before.plan.nodes() if n.kind == "append")
    assert len(append.children) == 12
    assert "no-partition-pruning" in {f.rule for f in before.findings}
    explanation_note = " ".join(n for s in before.explanation.steps for n in s.notes)
    assert "partitions" in explanation_note

    result = optimize_query(PARTITIONED, "postgres")
    assert "simplify" in {rw.rule for rw in result.rewrites}  # DATE_TRUNC('month', dt) = d -> range on dt
    after = analyzed(pg, result.optimized_sql, analyze=False)
    scanned = [n for n in after.plan.nodes() if n.kind == "scan"]
    assert len(scanned) == 1 and scanned[0].relation == "events_03"
    assert "no-partition-pruning" not in {f.rule for f in after.findings}
    assert rows(pg, result.optimized_sql) == rows(pg, PARTITIONED)


def test_hash_join_and_explanation_for_the_real_plan(pg):
    a = analyzed(pg, "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id WHERE c.region = 'EU'",
                 analyze=False)
    join = next(n for n in a.plan.nodes() if n.kind == "join")
    assert join.join_algo == "hash" and join.join_type == "INNER" and join.condition
    text = explain_plan(a.plan, a.findings).text()
    assert "PostgreSQL plan with the optimizer's estimates" in text
    assert "hash table" in text and "rows estimated" in text
    assert "Tables read: customers, orders" in text


def test_offline_plan_shape_matches_the_real_one(pg):
    """The connection-free plan predicts the operator order PostgreSQL actually picks."""
    sql = "SELECT c.region, count(*) FROM orders o JOIN customers c ON o.customer_id = c.id GROUP BY c.region ORDER BY 1"
    a = analyzed(pg, sql, analyze=False)
    offline_kinds = {n.kind for n in a.logical_plan.nodes()}
    real_kinds = {n.kind for n in a.plan.nodes()}
    # PostgreSQL adds parallelism (Gather Merge, partial aggregates) and may sort before a GroupAggregate,
    # but the operator *kinds* the offline plan predicts all show up in the real plan
    assert offline_kinds == {"sort", "aggregate", "join", "scan"} and offline_kinds <= real_kinds
    assert sum(n.kind == "scan" for n in a.plan.nodes()) == 2
    assert next(n for n in a.plan.nodes() if n.kind == "join").join_algo == \
        next(n for n in a.logical_plan.nodes() if n.kind == "join").join_algo == "hash"
