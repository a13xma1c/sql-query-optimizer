"""The demo environment: DuckDB population, DDL generation, schema hints, CLI."""

import duckdb
import pytest

from sqlanalyzer import demo
from sqlanalyzer.dialects import Engine
from sqlanalyzer.optimizer import parse_schema
from sqlanalyzer.parsing import parse_sql

SMALL = {"customers": 50, "products": 20, "orders": 300, "order_items": 400, "refunds": 30, "shipments": 100,
         "events": 200}


def test_populate_duckdb_creates_all_tables_with_keys_and_indexes(tmp_path):
    path = demo.populate_duckdb(tmp_path / "nested" / "demo.duckdb", sizes=SMALL)
    con = duckdb.connect(str(path), read_only=True)
    for table, rows in SMALL.items():
        assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == rows
    indexes = {r[0] for r in con.execute("SELECT index_name FROM duckdb_indexes()").fetchall()}
    assert {"orders_customer_idx", "orders_ts_idx", "order_items_order_idx", "products_sku_idx"} <= indexes
    keys = {(r[0], tuple(r[1])) for r in con.execute(
        "SELECT table_name, constraint_column_names FROM duckdb_constraints() WHERE constraint_type = 'PRIMARY KEY'").fetchall()}
    assert {("orders", ("id",)), ("customers", ("id",)), ("products", ("id",)), ("shipments", ("id",))} <= keys
    types = dict(con.execute("SELECT column_name, data_type FROM duckdb_columns() WHERE table_name = 'shipments'").fetchall())
    assert types["order_ref"] == "VARCHAR" and types["id"] == "INTEGER"
    assert con.execute("SELECT count(*) FROM customers WHERE name IS NULL").fetchone()[0] == 1  # NULLs on purpose
    con.close()


def test_populate_duckdb_replace_and_keep(tmp_path):
    path = tmp_path / "demo.duckdb"
    demo.populate_duckdb(path, sizes=SMALL)
    demo.populate_duckdb(path, sizes={**SMALL, "orders": 5}, replace=False)  # kept
    assert duckdb.connect(str(path), read_only=True).execute("SELECT count(*) FROM orders").fetchone()[0] == 300
    demo.populate_duckdb(path, sizes={**SMALL, "orders": 5}, replace=True)
    assert duckdb.connect(str(path), read_only=True).execute("SELECT count(*) FROM orders").fetchone()[0] == 5
    demo.populate_duckdb(":memory:", sizes=SMALL)  # must not try to unlink anything


def test_schema_hints_match_the_duckdb_demo(tmp_path):
    hints = parse_schema(demo.SCHEMA_HINTS)
    assert not hints.errors and set(hints.tables) == set(demo.TABLES)
    path = demo.populate_duckdb(tmp_path / "d.duckdb", sizes=SMALL)
    con = duckdb.connect(str(path), read_only=True)
    for table, columns in hints.tables.items():
        actual = [r[0] for r in con.execute(f"SELECT column_name FROM duckdb_columns() WHERE table_name = '{table}' "
                                            "ORDER BY column_index").fetchall()]
        assert actual == list(columns), table
    con.close()
    assert hints.stats["events"].partition == {"dt"} and hints.partition_hints() == {"events": ["dt"]}
    assert hints.stats["orders"].rows == demo.SIZES["orders"]


@pytest.mark.parametrize("engine", list(Engine))
def test_ddl_parses_in_the_target_dialect(engine):
    script = demo.ddl(engine, rows_per_table=3)
    dialect = "postgres" if engine is Engine.POSTGRES else engine.value
    statements = [s for s in script.split(";") if s.strip()]
    assert len(statements) >= 7
    for statement in statements:
        if statement.strip().upper().startswith(("CREATE", "INSERT")) and "PARTITION OF" not in statement.upper():
            assert parse_sql(statement, dialect).ok, statement[:80]
    assert "events" in script and "shipments" in script


def test_ddl_engine_specifics():
    assert "partitioning = ARRAY['month(dt)']" in demo.ddl(Engine.TRINO)
    assert "CLUSTER BY (dt)" in demo.ddl(Engine.SNOWFLAKE) and "TIMESTAMP_NTZ" in demo.ddl(Engine.SNOWFLAKE)
    assert "PARTITION BY RANGE (dt)" in demo.ddl(Engine.POSTGRES) and "events_12 PARTITION OF" in demo.ddl(Engine.POSTGRES)
    assert "range(1, 200000 + 1)" in demo.ddl(Engine.DUCKDB)


def test_cli(tmp_path, capsys, monkeypatch):
    target = tmp_path / "cli.duckdb"
    assert demo.main(["duckdb", str(target), "--scale", "0.01"]) == 0
    assert target.exists() and "ready" in capsys.readouterr().out
    assert duckdb.connect(str(target), read_only=True).execute("SELECT count(*) FROM orders").fetchone()[0] == 2000
    assert demo.main(["ddl", "trino", "--rows", "2"]) == 0 and "CREATE TABLE orders" in capsys.readouterr().out
    assert demo.main(["schema"]) == 0 and capsys.readouterr().out == demo.SCHEMA_HINTS
    monkeypatch.setenv("SQLANALYZER_DEMO_FILE", str(tmp_path / "env.duckdb"))
    assert demo.demo_path() == tmp_path / "env.duckdb"
    assert demo.main(["duckdb", "--scale", "0.005", "--keep"]) == 0 and (tmp_path / "env.duckdb").exists()


def test_populate_postgres_on_a_live_server(pg_server_or_skip):
    cfg = pg_server_or_skip
    demo.populate_postgres(cfg, sizes=SMALL, replace=True)
    from sqlalchemy import create_engine, text

    engine = create_engine(cfg.url())
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == SMALL["orders"]
            parts = conn.execute(text("SELECT count(*) FROM pg_inherits WHERE inhparent = 'events'::regclass")).scalar()
            assert parts == 12
            demo.populate_postgres(cfg, sizes={**SMALL, "orders": 1})  # already filled: kept
            assert conn.execute(text("SELECT count(*) FROM orders")).scalar() == SMALL["orders"]
    finally:
        engine.dispose()
    demo.drop_postgres(cfg)
    engine = create_engine(cfg.url())
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM pg_tables WHERE tablename = 'orders'")).scalar() == 0
    finally:
        engine.dispose()
