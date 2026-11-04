"""Catalog introspection: live DuckDB, canned rows for the other engines, round trip into schema hints."""

import duckdb
import pytest

from sqlanalyzer.catalog import CatalogOptions, _clustering_columns, introspect, introspect_with
from sqlanalyzer.db import ConnectionConfig, ExplainError
from sqlanalyzer.demo import populate_duckdb
from sqlanalyzer.dialects import Engine
from sqlanalyzer.optimizer import format_schema, parse_schema


@pytest.fixture(scope="module")
def duck_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("catalog") / "c.duckdb"
    con = duckdb.connect(str(path))
    con.execute("""
        CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, ts TIMESTAMP, status VARCHAR,
                             amount DECIMAL(10,2));
        CREATE UNIQUE INDEX ux ON orders(customer_id, ts);
        CREATE INDEX ix ON orders(status);
        INSERT INTO orders SELECT range, range % 10, TIMESTAMP '2024-01-01' + to_seconds(range), 'a', 1 FROM range(1000);
        CREATE TABLE customers (id INTEGER UNIQUE, name TEXT);
        CREATE VIEW v AS SELECT id FROM orders;
        CREATE SCHEMA other; CREATE TABLE other.t (a INTEGER);
    """)
    con.close()
    return str(path)


def test_duckdb_introspection(duck_path):
    result = introspect(ConnectionConfig(Engine.DUCKDB, database=duck_path))
    hints = result.hints
    assert set(hints.tables) == {"orders", "customers", "v"} and result.engine is Engine.DUCKDB
    assert hints.tables["orders"] == {"id": "INTEGER", "customer_id": "INTEGER", "ts": "TIMESTAMP", "status": "VARCHAR",
                                      "amount": "DECIMAL(10,2)"}
    stats = hints.stats["orders"]
    assert stats.rows == 1000 and stats.primary == {"id"} and stats.unique == {"id"}
    assert stats.indexed == {"id", "customer_id", "status"}  # leading columns of the indexes
    assert hints.stats["customers"].unique == {"id"} and hints.stats["customers"].rows == 0
    assert hints.stats["v"].rows is None and not result.skipped
    assert "3 table(s)" in result.summary()


def test_duckdb_options(duck_path):
    cfg = ConnectionConfig(Engine.DUCKDB, database=duck_path)
    only = introspect(cfg, CatalogOptions(tables=["orders"]))
    assert list(only.hints.tables) == ["orders"]
    with_ndv = introspect(cfg, CatalogOptions(tables=["orders"], distinct_counts=True))
    ndv = with_ndv.hints.stats["orders"].ndv
    assert ndv["customer_id"] == 10 and ndv["status"] == 1 and "id" not in ndv  # keys are skipped
    other = introspect(cfg, CatalogOptions(schemas=["other"]))
    assert list(other.hints.tables) == ["other.t"]
    capped = introspect(cfg, CatalogOptions(max_tables=1))
    assert len(capped.hints.tables) == 1


def test_introspection_round_trips_through_the_schema_text(duck_path):
    hints = introspect(ConnectionConfig(Engine.DUCKDB, database=duck_path)).hints
    text = format_schema(hints)
    assert "orders (rows=1000): id integer pk, customer_id integer indexed, ts timestamp, status varchar indexed" in text
    again = parse_schema(text)
    assert again.tables == hints.tables
    for table in hints.stats:
        assert again.stats[table].rows == hints.stats[table].rows
        assert again.stats[table].unique == hints.stats[table].unique
        assert again.stats[table].indexed == hints.stats[table].indexed


def test_demo_database_introspection(tmp_path):
    path = populate_duckdb(tmp_path / "demo.duckdb", sizes={"orders": 2000, "order_items": 2000, "shipments": 500,
                                                               "events": 500, "refunds": 100})
    hints = introspect(ConnectionConfig(Engine.DUCKDB, database=str(path))).hints
    assert set(hints.tables) == {"customers", "products", "orders", "order_items", "refunds", "shipments", "events"}
    assert hints.stats["orders"].rows == 2000 and hints.stats["orders"].indexed >= {"customer_id", "ts"}
    assert hints.stats["products"].unique == {"id", "sku"} and hints.stats["products"].primary == {"id"}
    assert hints.tables["shipments"]["order_ref"] == "VARCHAR" and hints.tables["events"]["dt"] == "DATE"


def test_connection_errors_are_reported(tmp_path):
    with pytest.raises(ExplainError):
        introspect(ConnectionConfig(Engine.DUCKDB, database=str(tmp_path / "no" / "dir" / "x.duckdb")))


# ------------------------------------------------------------ canned catalogs
class Canned:
    """Answers catalog queries from a list of (substring, rows) pairs."""

    def __init__(self, answers):
        self.answers = answers
        self.statements = []

    def __call__(self, sql):
        self.statements.append(sql)
        for needle, rows in self.answers:
            if needle.lower() in sql.lower():
                return rows if not isinstance(rows, Exception) else _raise(rows)
        return []


def _raise(exc):
    raise exc


def test_postgres_reader_from_canned_rows():
    run = Canned([
        ("current_schema", [("public",)]),
        ("FROM pg_class c JOIN pg_namespace", [("public", "orders", 2_000_000.0), ("public", "events", -1.0)]),
        ("FROM pg_attribute", [("public", "orders", "id", "integer"), ("public", "orders", "customer_id", "integer"),
                               ("public", "orders", "ts", "timestamp without time zone"),
                               ("public", "events", "dt", "date")]),
        ("FROM pg_index", [("public", "orders", True, True, ["id"]), ("public", "orders", False, False, ["ts"]),
                           ("public", "orders", False, False, None)]),
        ("FROM pg_stats", [("public", "orders", "customer_id", 1000.0), ("public", "orders", "ts", -0.5),
                           ("public", "orders", "id", -1.0), ("public", "nope", "x", 5.0)]),
        ("FROM pg_partitioned_table", [("public", "events", ["dt"])]),
    ])
    result = introspect_with(run, Engine.POSTGRES)
    orders, events = result.hints.stats["orders"], result.hints.stats["events"]
    assert orders.rows == 2_000_000 and events.rows is None  # -1 = never analyzed
    assert orders.primary == {"id"} and orders.indexed == {"id", "ts"}
    assert orders.ndv == {"customer_id": 1000, "ts": 1_000_000, "id": 2_000_000}  # negative = fraction of rows
    assert events.partition == {"dt"} and events.indexed == {"dt"}
    assert result.hints.tables["orders"]["ts"] == "TIMESTAMP WITHOUT TIME ZONE"
    assert not result.skipped
    assert "partitioned" in format_schema(result.hints)


def test_postgres_optional_sources_degrade_gracefully():
    run = Canned([
        ("current_schema", [("public",)]),
        ("FROM pg_class c JOIN pg_namespace", [("public", "t", 10.0)]),
        ("FROM pg_attribute", [("public", "t", "a", "integer")]),
        ("FROM pg_index", RuntimeError("permission denied")),
        ("FROM pg_stats", RuntimeError("no stats")),
    ])
    result = introspect_with(run, Engine.POSTGRES, CatalogOptions(distinct_counts=True))
    assert result.hints.tables == {"t": {"a": "INTEGER"}} and result.skipped == ["indexes", "statistics"]
    assert "not available: indexes, statistics" in result.summary()


def test_trino_reader_from_canned_rows():
    run = Canned([
        ("information_schema.columns", [("sales", "orders", "id", "integer"), ("sales", "orders", "ts", "timestamp(6)"),
                                        ("sales", "orders", "dt", "date")]),
        ('SHOW STATS FOR "orders"', [("id", None, 200000.0, 0.0, None, None, None),
                                     ("ts", None, 150000.0, 0.0, None, None, None),
                                     (None, None, None, None, 200000.0, None, None)]),
        ('SHOW COLUMNS FROM "orders"', [("id", "integer", "", ""), ("ts", "timestamp(6)", "", ""),
                                        ("dt", "date", "partition key", "")]),
    ])
    result = introspect_with(run, Engine.TRINO, default_schema="sales")
    orders = result.hints.stats["orders"]
    assert orders.rows == 200000 and orders.ndv == {"id": 200000, "ts": 150000}
    assert orders.partition == {"dt"} and result.hints.tables["orders"]["ts"] == "TIMESTAMP(6)"
    assert any("table_schema IN ('sales')" in s for s in run.statements)


def test_trino_iceberg_partitioning_from_show_create():
    run = Canned([
        ("information_schema.columns", [("s", "events", "ts", "timestamp(6)"), ("s", "events", "kind", "varchar")]),
        ("SHOW STATS FOR", RuntimeError("no stats")),
        ("SHOW COLUMNS FROM", [("ts", "timestamp(6)", "", ""), ("kind", "varchar", "", "")]),
        ("SHOW CREATE TABLE", [("CREATE TABLE s.events (...) WITH (partitioning = ARRAY['day(ts)', 'kind'])",)]),
    ])
    result = introspect_with(run, Engine.TRINO, default_schema="s")
    assert result.hints.stats["events"].partition == {"ts", "kind"}
    assert result.skipped == ["stats for events"]


def test_snowflake_reader_from_canned_rows():
    run = Canned([
        ("information_schema.tables", [("PUBLIC", "ORDERS", 5000000, "LINEAR(TO_DATE(TS), CUSTOMER_ID)"),
                                       ("PUBLIC", "CUSTOMERS", 1000, None)]),
        ("information_schema.columns", [("PUBLIC", "ORDERS", "ID", "NUMBER"), ("PUBLIC", "ORDERS", "TS", "TIMESTAMP_NTZ"),
                                        ("PUBLIC", "ORDERS", "CUSTOMER_ID", "NUMBER"),
                                        ("PUBLIC", "CUSTOMERS", "ID", "NUMBER"), ("PUBLIC", "CUSTOMERS", "NAME", "TEXT")]),
        ("SHOW PRIMARY KEYS", [(None, "DB", "PUBLIC", "ORDERS", "ID", 1, "PK_1"), (None, "DB", "PUBLIC", "CUSTOMERS", "ID", 1, "PK_2")]),
        ("SHOW UNIQUE KEYS", RuntimeError("not permitted")),
        ('FROM "orders"', [(12, 7)]),
    ])
    result = introspect_with(run, Engine.SNOWFLAKE, CatalogOptions(distinct_counts=True), default_schema="public")
    orders = result.hints.stats["orders"]
    assert orders.rows == 5_000_000 and orders.partition == {"ts", "customer_id"} and orders.primary == {"id"}
    assert result.hints.tables["customers"] == {"id": "NUMBER", "name": "TEXT"}
    assert result.skipped == ["unique keys"]
    assert any("APPROX_COUNT_DISTINCT" in s for s in run.statements)
    assert _clustering_columns('LINEAR("DT", region)') == ["dt", "region"] and _clustering_columns(None) == []


def test_table_filter_matches_bare_and_qualified_names():
    run = Canned([
        ("current_schema", [("main",)]),
        ("duckdb_tables", [("main", "orders", 5), ("main", "customers", 3)]),
        ("duckdb_views", []),
        ("duckdb_columns", [("main", "orders", "id", "INTEGER"), ("main", "customers", "id", "INTEGER")]),
        ("duckdb_constraints", []),
        ("duckdb_indexes", []),
    ])
    result = introspect_with(run, Engine.DUCKDB, CatalogOptions(tables=["ORDERS"]))
    assert list(result.hints.tables) == ["orders"]
