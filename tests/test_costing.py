"""The offline cost model: selectivity, cardinalities, access paths, engine families, assumptions."""

import pytest

from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.optimizer import optimize_query, parse_schema
from sqlanalyzer.parsing import parse_sql
from sqlanalyzer.plans.costing import DEFAULT_ROWS, CostModel, cost_delta, estimate_cost, fmt
from sqlanalyzer.plans.logical import build_logical_plan

HINTS = parse_schema("""
orders (rows=2000000): id int pk, customer_id int, ts timestamp, status text ndv=4, amount int
customers (rows=50000): id int pk, name text, region text ndv=3
refunds (rows=10000): order_id int, amt int
""")


def estimate(sql: str, engine: Engine = Engine.POSTGRES, stats=HINTS.stats):
    dialect = ENGINES[engine].sqlglot_dialect
    stmt = parse_sql(sql, dialect).statements[0]
    plan = build_logical_plan(stmt, engine)
    return estimate_cost(plan, engine, stats, stmt), plan


def node(plan, op_prefix: str):
    return next(n for n in plan.nodes() if n.op.startswith(op_prefix))


# ---------------------------------------------------------------------- basics
def test_plan_is_annotated_with_rows_and_costs():
    est, plan = estimate("SELECT o.id FROM orders o WHERE o.status = 'a'")
    assert all(n.est_rows is not None and n.cost is not None for n in plan.nodes())
    scan = node(plan, "Scan")
    assert scan.est_rows == pytest.approx(2_000_000 / 4)  # ndv=4 -> 1/4
    assert est.total_cost == plan.root.cost and est.output_rows == plan.root.est_rows
    assert est.table_rows == {"orders": 2_000_000.0} and est.assumptions == []
    assert "Estimated cost" in est.summary() and "dominant operator" in est.summary()
    assert plan.stats["cost_model"] == est.summary()
    assert est.share(scan.id) == pytest.approx(1.0)


def test_default_rows_and_assumptions_without_statistics():
    est, plan = estimate("SELECT a FROM t WHERE b = 1", stats=None)
    assert node(plan, "Scan").est_rows == pytest.approx(DEFAULT_ROWS * 0.1)
    assert any("assumed" in a and "rows=N" in a for a in est.assumptions)


@pytest.mark.parametrize("predicate, expected", [
    ("o.id = 5", 1 / 2_000_000),  # unique key
    ("o.status = 'a'", 1 / 4),  # declared ndv
    ("o.customer_id = 7", 0.01),  # id-like name
    ("o.amount = 7", 0.1),  # plain equality
    ("o.amount > 7", 1 / 3),
    ("o.amount >= 7 AND o.amount < 9", 0.1),  # both bounds of one interval
    ("o.amount BETWEEN 1 AND 2", 0.1),
    ("o.status IN ('a', 'b')", 0.5),
    ("o.status <> 'a'", 0.75),
    ("o.name LIKE 'ab%'", 0.1),
    ("o.name LIKE '%ab'", 0.5),
    ("o.amount IS NULL", 0.05),
    ("NOT o.amount IS NULL", 0.95),
    ("o.status = 'a' AND o.amount > 7", 1 / 4 / 3),
    ("o.status = 'a' OR o.amount > 7", 0.25 + 1 / 3 - 0.25 / 3),
    ("o.id IN (SELECT r.order_id FROM refunds r)", 0.5),
    ("TRUE", 1.0),
])
def test_selectivities(predicate, expected):
    _, plan = estimate(f"SELECT o.id FROM orders o WHERE {predicate}")
    rows = sum(n.est_rows for n in plan.nodes() if n.kind == "scan" and n.relation == "orders")
    filtered = plan.root.est_rows
    assert filtered == pytest.approx(2_000_000 * expected, rel=1e-3), (rows, filtered)


# ------------------------------------------------------------------ access paths
def test_access_paths():
    _, plan = estimate("SELECT o.id FROM orders o WHERE o.id = 5")
    assert node(plan, "Scan").attrs["access"] == "seek" and node(plan, "Scan").cost < 1000
    _, plan = estimate("SELECT o.id FROM orders o WHERE CAST(o.ts AS DATE) = DATE '2024-01-01'")
    assert node(plan, "Scan").attrs["access"] == "not-sargable"
    assert "no index or pruning" in node(plan, "Scan").attrs["model_note"]
    _, plan = estimate("SELECT o.id FROM orders o WHERE o.amount > 5")
    assert node(plan, "Scan").attrs["access"] == "scan"
    hints = parse_schema("orders (rows=2000000): id int pk, amount int indexed")
    _, plan = estimate("SELECT o.id FROM orders o WHERE o.amount > 5", stats=hints.stats)
    assert node(plan, "Scan").attrs["access"] == "prune" and "index range scan" in node(plan, "Scan").attrs["model_note"]


def test_columnar_engines_prune_on_time_columns_by_assumption():
    est, plan = estimate("SELECT o.id FROM orders o WHERE o.ts >= TIMESTAMP '2024-01-01 00:00:00'", Engine.DUCKDB)
    assert node(plan, "Scan").attrs["access"] == "prune"
    assert any("load order" in a for a in est.assumptions)
    est_pg, plan_pg = estimate("SELECT o.id FROM orders o WHERE o.ts >= TIMESTAMP '2024-01-01 00:00:00'")
    assert node(plan_pg, "Scan").attrs["access"] == "scan"


def test_columnar_scans_charge_only_referenced_columns():
    _, narrow = estimate("SELECT o.id FROM orders o", Engine.TRINO)
    _, wide = estimate("SELECT * FROM orders o", Engine.TRINO)
    _, row_store = estimate("SELECT o.id FROM orders o")
    assert node(narrow, "Scan").cost < node(wide, "Scan").cost < node(row_store, "Scan").cost
    assert "20% of the columns" in node(narrow, "Scan").attrs["model_note"]


# ------------------------------------------------------------------------ joins
def test_join_cardinality_uses_keys():
    _, plan = estimate("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id")
    join = node(plan, "Hash Join")
    assert join.est_rows == pytest.approx(2_000_000)  # customers.id is unique: orders rows survive
    assert "hash join" in join.attrs["model_note"]
    est, plan = estimate("SELECT 1 FROM orders o JOIN refunds r ON o.amount = r.amt")
    assert node(plan, "Hash Join").est_rows == pytest.approx(2_000_000)
    assert any("foreign-key assumption" in a for a in est.assumptions)
    _, plan = estimate("SELECT 1 FROM orders o, customers c")
    cross = node(plan, "Cross Join")
    assert cross.est_rows == pytest.approx(2_000_000 * 50_000) and cross.cost >= cross.est_rows
    _, plan = estimate("SELECT 1 FROM orders o JOIN customers c ON o.amount > c.id")
    assert "nested loop" in node(plan, "Nested Loop").attrs["model_note"]
    _, plan = estimate("SELECT 1 FROM customers c LEFT JOIN refunds r ON r.order_id = c.id")
    assert node(plan, "Hash Join").est_rows >= 50_000


def test_distributed_engines_pay_for_shuffles_and_global_sorts():
    sql = "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id ORDER BY o.id"
    _, trino = estimate(sql, Engine.TRINO)
    _, duck = estimate(sql, Engine.DUCKDB)
    assert "shuffle" in node(trino, "Hash Join").attrs["model_note"]
    assert node(trino, "Hash Join").attrs["model_cost"] > node(duck, "Hash Join").attrs["model_cost"]
    assert node(trino, "Sort").attrs["model_cost"] > node(duck, "Sort").attrs["model_cost"]


# -------------------------------------------------------------------- operators
def test_aggregate_sort_limit_window_and_set_operations():
    _, plan = estimate("SELECT o.status, count(*) FROM orders o GROUP BY o.status")
    agg = node(plan, "Aggregate")
    assert 1 < agg.est_rows < 2_000_000 and "hash aggregate" in agg.attrs["model_note"]
    _, plan = estimate("SELECT count(*) FROM orders o")
    assert node(plan, "Aggregate").est_rows == 1
    _, plan = estimate("SELECT o.id FROM orders o ORDER BY o.id LIMIT 10")
    assert node(plan, "Limit").est_rows == 10 and "top-N" in node(plan, "Top-N").attrs["model_note"]
    _, plan = estimate("SELECT DISTINCT o.status FROM orders o")
    assert node(plan, "Distinct").est_rows < 2_000_000
    _, plan = estimate("SELECT o.id, row_number() OVER (ORDER BY o.ts) FROM orders o")
    assert node(plan, "Window").est_rows == 2_000_000 and node(plan, "Window").cost > 2_000_000
    _, plan = estimate("SELECT id FROM orders UNION SELECT order_id FROM refunds")
    assert node(plan, "Union").est_rows == pytest.approx(2_010_000 * 0.9)
    _, plan = estimate("SELECT id FROM orders UNION ALL SELECT order_id FROM refunds")
    assert node(plan, "Union All").est_rows == pytest.approx(2_010_000)
    _, plan = estimate("WITH r AS (SELECT order_id FROM refunds) SELECT o.id FROM orders o JOIN r ON r.order_id = o.id")
    assert "materialisation" in node(plan, "CTE").attrs["model_note"]
    _, plan = estimate("DELETE FROM orders WHERE id = 1")
    assert "writes" in node(plan, "Delete").attrs["model_note"]


# ------------------------------------------------------------ comparing queries
def test_rewrite_and_declared_index_lower_the_estimate():
    sql = ("SELECT o.id, c.name FROM orders o, customers c WHERE o.customer_id = c.id "
           "AND CAST(o.ts AS DATE) = DATE '2024-01-15' AND c.region = 'EU'")
    before, _ = estimate(sql, Engine.SNOWFLAKE)
    rewritten = optimize_query(sql, "snowflake").optimized_sql
    after, _ = estimate(rewritten, Engine.SNOWFLAKE)
    assert after.total_cost < before.total_cost
    assert cost_delta(before, after).endswith("%)") and "->" in cost_delta(before, after)
    indexed = parse_schema("orders (rows=2000000): id int pk, customer_id int, ts timestamp indexed\n"
                           "customers (rows=50000): id int pk, name text, region text")
    pg_after, _ = estimate(optimize_query(sql, "postgres").optimized_sql, Engine.POSTGRES, indexed.stats)
    pg_before, _ = estimate(sql, Engine.POSTGRES, indexed.stats)
    assert pg_after.total_cost < pg_before.total_cost * 0.5
    assert cost_delta(None, after) is None and cost_delta(before, None) is None


def test_fmt_and_model_reuse():
    assert fmt(None) == "?" and fmt(12) == "12" and fmt(12_500) == "12.5K" and fmt(2.4e6) == "2.4M" and fmt(3e9) == "3.0B"
    model = CostModel(Engine.DUCKDB, HINTS.stats)
    first = model.estimate(build_logical_plan(parse_sql("SELECT a FROM t", "duckdb").statements[0], Engine.DUCKDB))
    second = model.estimate(build_logical_plan(parse_sql("SELECT o.id FROM orders o", "duckdb").statements[0], Engine.DUCKDB))
    assert any("assumed" in a for a in first.assumptions) and second.assumptions == []  # state is reset per plan


def test_unparseable_filter_falls_back_gracefully():
    from sqlanalyzer.plans.model import PlanNode

    model = CostModel(Engine.POSTGRES, HINTS.stats)
    sel, note = model._selectivity("this is (not sql", None)
    assert sel == 0.5 and "unknown" in note
    assert model._access_path("this is (not sql", None) == "scan"
    assert model._node(PlanNode(0, "Mystery", "other"))[0] == 0.0
