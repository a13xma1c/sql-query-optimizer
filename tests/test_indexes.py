"""Index / physical-design recommendations per engine."""

import pytest

from sqlanalyzer.analyzer import analyze_query
from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.indexes import recommend_indexes
from sqlanalyzer.optimizer import parse_schema
from sqlanalyzer.parsing import parse_sql

HINTS = parse_schema("""
orders (rows=2000000): id int pk, customer_id int, ts timestamp, status text ndv=4, amount int, s text
customers (rows=50000): id int pk, name text, region text ndv=3
refunds (rows=10000): order_id int, amt int
""")
SQL = ("SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id "
       "WHERE CAST(o.ts AS DATE) = DATE '2024-01-15' AND c.region = 'EU' AND lower(c.name) LIKE 'a%' "
       "AND o.status IN ('a', 'b') AND o.amount > 10 ORDER BY o.ts LIMIT 10")


def recs(sql: str, engine: Engine, stats=HINTS.stats):
    dialect = ENGINES[engine].sqlglot_dialect
    return recommend_indexes(parse_sql(sql, dialect).statements, engine, stats, dialect=dialect)


def by_kind(items, kind):
    return [r for r in items if r.kind == kind]


def test_postgres_recommendations():
    out = recs(SQL, Engine.POSTGRES)
    btree = {r.columns: r for r in by_kind(out, "btree")}
    assert ("status", "amount") in btree  # equality first, then the range column
    assert btree[("status", "amount")].priority == "high" and "o.status IN" in btree[("status", "amount")].reason
    assert btree[("status", "amount")].ddl == "CREATE INDEX ix_orders_status_amount ON orders (status, amount);"
    assert ("ts",) in btree and "rewrites that as a range" in btree[("ts",)].reason
    assert btree[("ts",)].fixes == ("non-sargable-predicate",)
    assert ("customer_id",) in btree and "join key" in btree[("customer_id",)].reason
    assert ("region",) in btree and btree[("region",)].priority == "medium"  # 50k rows: not large
    trigram = by_kind(out, "trigram")
    assert trigram and "gin_trgm_ops" in trigram[0].ddl and trigram[0].columns == ("name",)
    expression = by_kind(out, "expression")
    assert expression and expression[0].ddl == "CREATE INDEX ix_orders_casttsasdate ON orders ((CAST(ts AS DATE)));"
    assert expression[0].priority == "low"
    assert [r.priority for r in out] == sorted((r.priority for r in out), key=["high", "medium", "low"].index)


def test_duckdb_recommendations():
    out = recs(SQL, Engine.DUCKDB)
    sort = by_kind(out, "sort-order")
    assert sort and sort[0].table == "orders" and sort[0].columns[0] == "ts"
    assert "rewrite 'CAST(o.ts AS DATE)'" in sort[0].reason and sort[0].priority == "high"
    assert not by_kind(out, "art")  # status/amount are not selective point lookups
    art = by_kind(recs("SELECT * FROM orders o WHERE o.customer_id = 7", Engine.DUCKDB), "art")
    assert art and art[0].ddl == "CREATE INDEX ix_orders_customer_id ON orders (customer_id);"


def test_trino_recommendations():
    out = recs(SQL, Engine.TRINO)
    partition = by_kind(out, "partition")
    assert partition and partition[0].columns == ("ts",) and "day(ts)" in partition[0].ddl
    assert "no-partition-pruning" in partition[0].fixes and "non-sargable-predicate" in partition[0].fixes
    sorted_by = {r.table: r for r in by_kind(out, "sorted-by")}
    assert "customer_id" in sorted_by["orders"].columns and "bucketed_by" in sorted_by["orders"].ddl


def test_snowflake_recommendations():
    out = recs(SQL, Engine.SNOWFLAKE)
    cluster = {r.table: r for r in by_kind(out, "clustering")}
    assert cluster["orders"].columns[0] == "ts" and cluster["orders"].ddl.startswith("ALTER TABLE orders CLUSTER BY (ts")
    assert "poor-partition-pruning" in cluster["orders"].fixes
    search = by_kind(recs("SELECT * FROM orders o WHERE o.customer_id = 7", Engine.SNOWFLAKE), "search-optimization")
    assert search and "SEARCH OPTIMIZATION ON EQUALITY(customer_id)" in search[0].ddl


def test_keys_and_declared_indexes_are_not_recommended_again():
    assert recs("SELECT * FROM orders o WHERE o.id = 1", Engine.POSTGRES) == []
    hints = parse_schema("orders (rows=2000000): id int pk, status text indexed")
    assert recs("SELECT * FROM orders o WHERE o.status = 'a'", Engine.POSTGRES, hints.stats) == []
    assert recs("SELECT * FROM orders o WHERE o.status = 'a'", Engine.POSTGRES) != []


def test_priority_follows_table_size():
    big = recs("SELECT * FROM orders o WHERE o.status = 'a'", Engine.POSTGRES)
    small = recs("SELECT * FROM refunds r WHERE r.amt = 1", Engine.POSTGRES)
    unknown = recs("SELECT * FROM t WHERE t.x = 1", Engine.POSTGRES, {})
    assert big[0].priority == "high" and small[0].priority == "medium" and unknown[0].priority == "high"
    assert big[0].table_rows == 2_000_000 and unknown[0].table_rows is None


def test_order_by_only_and_no_predicates():
    out = recs("SELECT * FROM orders o ORDER BY o.amount", Engine.POSTGRES)
    assert out and out[0].columns == ("amount",) and out[0].priority == "low"
    assert recs("SELECT count(*) FROM orders", Engine.POSTGRES) == []
    assert recs("SELECT 1", Engine.POSTGRES) == []


def test_outer_join_keys_and_composite_predicates():
    out = recs("SELECT * FROM orders o LEFT JOIN refunds r ON r.order_id = o.id WHERE o.status = 'a' OR o.amount = 1",
               Engine.POSTGRES)
    cols = {r.columns for r in out}
    assert ("order_id",) in cols  # join key of the outer join still matters
    assert ("status",) not in cols and ("amount",) not in cols  # OR-ed predicates are not index candidates


def test_recommendations_are_deduplicated_across_query_blocks():
    sql = ("SELECT * FROM orders o WHERE o.status = 'a' "
           "UNION ALL SELECT * FROM orders o WHERE o.status = 'b' AND o.id IN (SELECT r.order_id FROM refunds r WHERE r.amt = 3)")
    out = recs(sql, Engine.POSTGRES)
    assert len([r for r in out if r.columns == ("status",)]) == 1
    assert any(r.table == "refunds" and r.columns == ("amt",) for r in out)


@pytest.mark.parametrize("engine", list(Engine))
def test_analysis_exposes_recommendations_and_cost_with_indexes(engine):
    a = analyze_query(SQL, engine, stats=HINTS.stats, optimize=False)
    assert a.index_recommendations and a.cost is not None and a.cost_with_indexes is not None
    assert a.cost_with_indexes.total_cost <= a.cost.total_cost
    assert a.cost_change_with_indexes and "->" in a.cost_change_with_indexes
    assert all(r.engine is engine for r in a.index_recommendations)
