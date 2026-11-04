"""The demo environment: a small e-commerce schema with enough rows to make plans interesting.

Tables (the same on every engine)::

    customers   1 000     id pk, name, region, signup_date, email
    products      500     id pk, sku unique, category, price
    orders    200 000     id pk, customer_id, ts, status, amount        (indexes on customer_id, ts)
    order_items 400 000   order_id, product_id, quantity, price
    refunds     5 000     order_id, amt, reason
    shipments 150 000     id pk, order_ref VARCHAR (!), shipped_at, carrier   -> join-key type mismatch sample
    events    120 000     id, dt, user_id, kind                          (range-partitioned by month on PostgreSQL)

Use it from the command line::

    python -m sqlanalyzer.demo duckdb demo.duckdb          # create/populate a DuckDB file
    python -m sqlanalyzer.demo postgres postgresql+psycopg2://user@host/db
    python -m sqlanalyzer.demo ddl trino > demo_trino.sql  # DDL + sample INSERTs for engines that cannot run here
    python -m sqlanalyzer.demo schema                      # the schema hints text for the Schema tab

or from the GUI (*Connection… → Use demo database*).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .db import DUCKDB_STORAGE_VERSION, ConnectionConfig
from .dialects import Engine

SIZES = {"customers": 1_000, "products": 500, "orders": 200_000, "order_items": 400_000, "refunds": 5_000,
         "shipments": 150_000, "events": 120_000}
TABLES = tuple(SIZES)

# Schema hints as the Schema tab expects them (what introspection of the demo database produces, roughly).
SCHEMA_HINTS = """\
customers (rows=1000): id integer pk, name varchar, region varchar ndv=3, signup_date date, email varchar
products (rows=500): id integer pk, sku varchar unique, category varchar ndv=8, price decimal(10,2)
orders (rows=200000): id integer pk, customer_id integer indexed, ts timestamp indexed, status varchar ndv=4, amount integer
order_items (rows=400000): order_id integer indexed, product_id integer, quantity integer, price decimal(10,2)
refunds (rows=5000): order_id integer, amt integer, reason varchar ndv=3
shipments (rows=150000): id integer pk, order_ref varchar, shipped_at timestamp, carrier varchar ndv=4
events (rows=120000): id bigint, dt date partitioned, user_id integer, kind varchar ndv=3
"""

PARTITION_HINTS = "events: dt\n"


def demo_path() -> Path:
    """Default location of the demo DuckDB file (next to the settings)."""
    override = os.environ.get("SQLANALYZER_DEMO_FILE")
    if override:
        return Path(override)
    try:
        from .gui.settings import default_path

        return default_path().with_name("demo.duckdb")
    except ImportError:  # pragma: no cover - GUI extras not installed
        return Path.home() / ".config" / "SQLAnalyzer" / "demo.duckdb"


# --------------------------------------------------------------------- DuckDB
DUCKDB_SQL = """
CREATE TABLE customers AS
    SELECT CAST(i AS INTEGER) AS id, CASE WHEN i % 50 = 0 THEN NULL ELSE 'customer ' || i END AS name,
           ['EU', 'US', 'APAC'][1 + i % 3] AS region,
           CAST(DATE '2020-01-01' + INTERVAL (i % 1500) DAY AS DATE) AS signup_date,
           'user' || i || '@example.com' AS email
    FROM range(1, {customers} + 1) t(i);
CREATE TABLE products AS
    SELECT CAST(i AS INTEGER) AS id, 'SKU-' || lpad(CAST(i AS VARCHAR), 5, '0') AS sku,
           ['books', 'toys', 'garden', 'tools', 'food', 'music', 'games', 'home'][1 + i % 8] AS category,
           CAST(5 + (i % 200) * 1.5 AS DECIMAL(10, 2)) AS price
    FROM range(1, {products} + 1) t(i);
CREATE TABLE orders AS
    SELECT CAST(i AS INTEGER) AS id, CAST(1 + i % {customers} AS INTEGER) AS customer_id,
           TIMESTAMP '2024-01-01 00:00:00' + INTERVAL (i % 365) DAY + INTERVAL (i % 24) HOUR AS ts,
           ['a', 'b', 'c', 'd'][1 + i % 4] AS status, CAST(i % 500 AS INTEGER) AS amount
    FROM range(1, {orders} + 1) t(i);
CREATE TABLE order_items AS
    SELECT CAST(1 + i % {orders} AS INTEGER) AS order_id, CAST(1 + i % {products} AS INTEGER) AS product_id,
           CAST(1 + i % 5 AS INTEGER) AS quantity, CAST(5 + (i % 200) * 1.5 AS DECIMAL(10, 2)) AS price
    FROM range(1, {order_items} + 1) t(i);
CREATE TABLE refunds AS
    SELECT CAST(i * 37 AS INTEGER) AS order_id, CAST(i % 9 AS INTEGER) AS amt,
           ['damaged', 'late', 'other'][1 + i % 3] AS reason
    FROM range(1, {refunds} + 1) t(i);
CREATE TABLE shipments AS
    SELECT CAST(i AS INTEGER) AS id, CAST(1 + i % {orders} AS VARCHAR) AS order_ref,
           TIMESTAMP '2024-01-02 00:00:00' + INTERVAL (i % 365) DAY AS shipped_at,
           ['dhl', 'ups', 'fedex', 'post'][1 + i % 4] AS carrier
    FROM range(1, {shipments} + 1) t(i);
CREATE TABLE events AS
    SELECT i AS id, CAST(DATE '2024-01-01' + INTERVAL (i % 366) DAY AS DATE) AS dt, CAST(i % 5000 AS INTEGER) AS user_id,
           ['view', 'click', 'buy'][1 + i % 3] AS kind
    FROM range(1, {events} + 1) t(i);
ALTER TABLE customers ADD PRIMARY KEY (id);
ALTER TABLE products ADD PRIMARY KEY (id);
CREATE UNIQUE INDEX products_sku_idx ON products (sku);
ALTER TABLE orders ADD PRIMARY KEY (id);
CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_ts_idx ON orders (ts);
CREATE INDEX order_items_order_idx ON order_items (order_id);
ALTER TABLE shipments ADD PRIMARY KEY (id);
"""


def populate_duckdb(path: str | os.PathLike[str], *, sizes: dict[str, int] | None = None,
                    replace: bool = True, storage_version: str = DUCKDB_STORAGE_VERSION) -> Path:
    """Create the demo tables in a DuckDB file (or ``":memory:"``); returns the path.

    The file is written in the ``storage_version`` format (default ``v1.0.0``) so that
    older DuckDB builds and third-party tools can open it - DuckDB 2.x would otherwise
    use its latest format (header version 999), which nothing else reads.
    """
    import duckdb

    sizes = {**SIZES, **(sizes or {})}
    target = Path(path)
    if replace and str(path) != ":memory:" and target.exists():
        target.unlink()
    if str(path) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path), config={"storage_compatibility_version": storage_version})
    try:
        if _duckdb_has_tables(con) and not replace:
            return target
        for statement in DUCKDB_SQL.format(**sizes).split(";"):
            if statement.strip():
                con.execute(statement)
    finally:
        con.close()
    return target


def _duckdb_has_tables(con) -> bool:
    rows = con.execute("SELECT table_name FROM duckdb_tables() WHERE NOT internal").fetchall()
    return {r[0] for r in rows} >= set(TABLES)


# ------------------------------------------------------------------ PostgreSQL
POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (id int PRIMARY KEY, name text, region text, signup_date date, email text);
CREATE TABLE IF NOT EXISTS products (id int PRIMARY KEY, sku varchar(16) UNIQUE, category text, price numeric(10, 2));
CREATE TABLE IF NOT EXISTS orders (id int PRIMARY KEY, customer_id int, ts timestamp, status text, amount int);
CREATE TABLE IF NOT EXISTS order_items (order_id int, product_id int, quantity int, price numeric(10, 2));
CREATE TABLE IF NOT EXISTS refunds (order_id int, amt int, reason text);
CREATE TABLE IF NOT EXISTS shipments (id int PRIMARY KEY, order_ref varchar(16), shipped_at timestamp, carrier text);
CREATE TABLE IF NOT EXISTS events (id bigint, dt date, user_id int, kind text) PARTITION BY RANGE (dt);
"""
POSTGRES_DATA = """
INSERT INTO customers
    SELECT i, CASE WHEN i % 50 = 0 THEN NULL ELSE 'customer ' || i END, (ARRAY['EU', 'US', 'APAC'])[1 + i % 3],
           DATE '2020-01-01' + (i % 1500), 'user' || i || '@example.com'
    FROM generate_series(1, {customers}) i;
INSERT INTO products
    SELECT i, 'SKU-' || lpad(i::text, 5, '0'),
           (ARRAY['books', 'toys', 'garden', 'tools', 'food', 'music', 'games', 'home'])[1 + i % 8], 5 + (i % 200) * 1.5
    FROM generate_series(1, {products}) i;
INSERT INTO orders
    SELECT i, 1 + i % {customers}, TIMESTAMP '2024-01-01' + (i % 365) * INTERVAL '1 day' + (i % 24) * INTERVAL '1 hour',
           (ARRAY['a', 'b', 'c', 'd'])[1 + i % 4], i % 500
    FROM generate_series(1, {orders}) i;
INSERT INTO order_items
    SELECT 1 + i % {orders}, 1 + i % {products}, 1 + i % 5, 5 + (i % 200) * 1.5 FROM generate_series(1, {order_items}) i;
INSERT INTO refunds SELECT i * 37, i % 9, (ARRAY['damaged', 'late', 'other'])[1 + i % 3] FROM generate_series(1, {refunds}) i;
INSERT INTO shipments
    SELECT i, (1 + i % {orders})::text, TIMESTAMP '2024-01-02' + (i % 365) * INTERVAL '1 day',
           (ARRAY['dhl', 'ups', 'fedex', 'post'])[1 + i % 4]
    FROM generate_series(1, {shipments}) i;
INSERT INTO events
    SELECT i, DATE '2024-01-01' + (i % 366), i % 5000, (ARRAY['view', 'click', 'buy'])[1 + i % 3]
    FROM generate_series(1, {events}) i;
CREATE INDEX IF NOT EXISTS orders_customer_idx ON orders (customer_id);
CREATE INDEX IF NOT EXISTS orders_ts_idx ON orders (ts);
CREATE INDEX IF NOT EXISTS order_items_order_idx ON order_items (order_id);
ANALYZE;
"""


def postgres_partitions() -> list[str]:
    out = []
    for month in range(1, 13):
        start = f"2024-{month:02d}-01"
        end = "2025-01-01" if month == 12 else f"2024-{month + 1:02d}-01"
        out.append(f"CREATE TABLE IF NOT EXISTS events_{month:02d} PARTITION OF events "
                   f"FOR VALUES FROM ('{start}') TO ('{end}')")
    return out


def populate_postgres(target: str | ConnectionConfig, *, sizes: dict[str, int] | None = None,
                      replace: bool = False) -> None:
    """Create (and fill, if empty) the demo tables in a PostgreSQL database given by URL or config."""
    from sqlalchemy import create_engine, text

    sizes = {**SIZES, **(sizes or {})}
    cfg = target if isinstance(target, ConnectionConfig) else ConnectionConfig.from_url(Engine.POSTGRES, target)
    engine = create_engine(cfg.url())
    try:
        with engine.begin() as conn:
            if replace:
                for table in reversed(TABLES):
                    conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
            for statement in _statements(POSTGRES_SCHEMA):
                conn.execute(text(statement))
            for statement in postgres_partitions():
                conn.execute(text(statement))
            if conn.execute(text("SELECT count(*) FROM orders")).scalar() == 0:
                for statement in _statements(POSTGRES_DATA.format(**sizes)):
                    conn.execute(text(statement))
    finally:
        engine.dispose()


def drop_postgres(target: str | ConnectionConfig) -> None:
    from sqlalchemy import create_engine, text

    cfg = target if isinstance(target, ConnectionConfig) else ConnectionConfig.from_url(Engine.POSTGRES, target)
    engine = create_engine(cfg.url())
    try:
        with engine.begin() as conn:
            for table in reversed(TABLES):
                conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
    finally:
        engine.dispose()


def _statements(script: str) -> list[str]:
    return [s.strip() for s in script.split(";") if s.strip()]


# ------------------------------------------------------ Trino / Snowflake DDL
def ddl(engine: Engine, rows_per_table: int = 20) -> str:
    """Portable DDL plus a few INSERT ... VALUES rows for engines this machine cannot run.

    The INSERTs are small on purpose (they are examples, not a load script); use
    ``rows_per_table`` to generate more.
    """
    if engine is Engine.TRINO:
        types = {"int": "integer", "bigint": "bigint", "text": "varchar", "vc": "varchar(16)", "num": "decimal(10, 2)",
                 "ts": "timestamp", "date": "date"}
        with_ = {"events": " WITH (partitioning = ARRAY['month(dt)'])",
                 "orders": " WITH (sorted_by = ARRAY['ts'])"}
    elif engine is Engine.SNOWFLAKE:
        types = {"int": "NUMBER(10,0)", "bigint": "NUMBER(19,0)", "text": "VARCHAR", "vc": "VARCHAR(16)",
                 "num": "NUMBER(10,2)", "ts": "TIMESTAMP_NTZ", "date": "DATE"}
        with_ = {"events": " CLUSTER BY (dt)", "orders": " CLUSTER BY (ts)"}
    elif engine is Engine.POSTGRES:
        return POSTGRES_SCHEMA.strip() + "\n" + ";\n".join(postgres_partitions()) + ";\n" + POSTGRES_DATA.format(**SIZES)
    else:
        return DUCKDB_SQL.format(**SIZES).strip() + "\n"
    t = types
    lines = [
        f"CREATE TABLE customers (id {t['int']}, name {t['text']}, region {t['text']}, signup_date {t['date']}, email {t['text']});",
        f"CREATE TABLE products (id {t['int']}, sku {t['vc']}, category {t['text']}, price {t['num']});",
        f"CREATE TABLE orders (id {t['int']}, customer_id {t['int']}, ts {t['ts']}, status {t['text']}, amount {t['int']}){with_['orders']};",
        f"CREATE TABLE order_items (order_id {t['int']}, product_id {t['int']}, quantity {t['int']}, price {t['num']});",
        f"CREATE TABLE refunds (order_id {t['int']}, amt {t['int']}, reason {t['text']});",
        f"CREATE TABLE shipments (id {t['int']}, order_ref {t['vc']}, shipped_at {t['ts']}, carrier {t['text']});",
        f"CREATE TABLE events (id {t['bigint']}, dt {t['date']}, user_id {t['int']}, kind {t['text']}){with_['events']};",
        "",
    ]
    lines += _sample_inserts(engine, rows_per_table)
    return "\n".join(lines) + "\n"


def _sample_inserts(engine: Engine, n: int) -> list[str]:
    regions, statuses, kinds = ["EU", "US", "APAC"], ["a", "b", "c", "d"], ["view", "click", "buy"]
    carriers, reasons = ["dhl", "ups", "fedex", "post"], ["damaged", "late", "other"]
    cats = ["books", "toys", "garden", "tools", "food", "music", "games", "home"]
    ts = "TIMESTAMP" if engine is Engine.TRINO else "TIMESTAMP_NTZ"

    def rows(values):
        return ",\n    ".join(values)

    return [
        "INSERT INTO customers VALUES\n    " + rows(
            f"({i}, 'customer {i}', '{regions[i % 3]}', DATE '2020-01-{1 + i % 28:02d}', 'user{i}@example.com')"
            for i in range(1, n + 1)) + ";",
        "INSERT INTO products VALUES\n    " + rows(
            f"({i}, 'SKU-{i:05d}', '{cats[i % 8]}', {5 + (i % 200) * 1.5:.2f})" for i in range(1, n + 1)) + ";",
        "INSERT INTO orders VALUES\n    " + rows(
            f"({i}, {1 + i % n}, {ts} '2024-01-{1 + i % 28:02d} {i % 24:02d}:00:00', '{statuses[i % 4]}', {i % 500})"
            for i in range(1, n + 1)) + ";",
        "INSERT INTO order_items VALUES\n    " + rows(
            f"({1 + i % n}, {1 + i % n}, {1 + i % 5}, {5 + (i % 200) * 1.5:.2f})" for i in range(1, n + 1)) + ";",
        "INSERT INTO refunds VALUES\n    " + rows(
            f"({i * 3}, {i % 9}, '{reasons[i % 3]}')" for i in range(1, max(n // 4, 1) + 1)) + ";",
        "INSERT INTO shipments VALUES\n    " + rows(
            f"({i}, '{1 + i % n}', {ts} '2024-01-{1 + i % 28:02d} 00:00:00', '{carriers[i % 4]}')"
            for i in range(1, n + 1)) + ";",
        "INSERT INTO events VALUES\n    " + rows(
            f"({i}, DATE '2024-{1 + i % 12:02d}-{1 + i % 28:02d}', {i % 5000}, '{kinds[i % 3]}')"
            for i in range(1, n + 1)) + ";",
    ]


# ------------------------------------------------------------------------ CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m sqlanalyzer.demo", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    duck = sub.add_parser("duckdb", help="create/populate a DuckDB file")
    duck.add_argument("path", nargs="?", default=None, help="file path (default: the app's demo.duckdb)")
    duck.add_argument("--keep", action="store_true", help="do not replace an existing file")
    duck.add_argument("--scale", type=float, default=1.0, help="multiply the row counts (e.g. 0.1 or 5)")
    duck.add_argument("--storage-version", default=DUCKDB_STORAGE_VERSION,
                      help="DuckDB storage format to write (default v1.0.0: readable by DuckDB >= 1.0)")
    pg = sub.add_parser("postgres", help="create/populate the demo tables in a PostgreSQL database")
    pg.add_argument("url", help="SQLAlchemy URL, e.g. postgresql+psycopg2://user:pw@host:5432/db")
    pg.add_argument("--replace", action="store_true", help="drop and recreate the tables first")
    pg.add_argument("--drop", action="store_true", help="only drop the demo tables")
    pg.add_argument("--scale", type=float, default=1.0)
    ddl_cmd = sub.add_parser("ddl", help="print DDL (+ sample rows) for an engine")
    ddl_cmd.add_argument("engine", choices=[e.value for e in Engine])
    ddl_cmd.add_argument("--rows", type=int, default=20)
    sub.add_parser("schema", help="print the schema hints for the Schema tab")
    args = parser.parse_args(argv)

    if args.command == "duckdb":
        sizes = {k: max(1, int(v * args.scale)) for k, v in SIZES.items()}
        path = populate_duckdb(args.path or demo_path(), sizes=sizes, replace=not args.keep,
                               storage_version=args.storage_version)
        print(f"DuckDB demo database ready: {path} (storage format {args.storage_version})")
    elif args.command == "postgres":
        if args.drop:
            drop_postgres(args.url)
            print("Demo tables dropped.")
        else:
            sizes = {k: max(1, int(v * args.scale)) for k, v in SIZES.items()}
            populate_postgres(args.url, sizes=sizes, replace=args.replace)
            print("PostgreSQL demo tables ready.")
    elif args.command == "ddl":
        sys.stdout.write(ddl(Engine(args.engine), args.rows))
    elif args.command == "schema":
        sys.stdout.write(SCHEMA_HINTS)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
