"""End-to-end fixtures: real database servers where they can run here, recorded ones otherwise.

Engines
-------
* **DuckDB** - always live (in-process, a temporary file).
* **PostgreSQL** - live: ``SQLANALYZER_E2E_POSTGRES_URL`` if set, otherwise a throw-away cluster started
  with the local ``initdb``/``pg_ctl`` binaries (skipped when neither is available).
* **Trino / Snowflake** - live when ``SQLANALYZER_E2E_TRINO_URL`` / ``SQLANALYZER_E2E_SNOWFLAKE_URL`` point at a
  server that has the sample tables; otherwise a *recorded server*: the application still builds the SQLAlchemy
  URL, issues the engine's EXPLAIN statement and parses rows in the driver's shape, but the rows come from the
  fixtures in ``tests/data`` instead of a network round trip.

Every live server gets the same sample schema (``customers``, ``orders`` with 200k rows, ``refunds``; PostgreSQL
additionally a range-partitioned ``events`` table) so the same queries can be explained everywhere.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError

from sqlanalyzer.db import ConnectionConfig
from sqlanalyzer.demo import SIZES, drop_postgres, populate_duckdb, populate_postgres
from sqlanalyzer.dialects import ENGINES, Engine, explain_statement

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DATA = Path(__file__).resolve().parent.parent / "data"
ENV_URLS = {
    Engine.POSTGRES: "SQLANALYZER_E2E_POSTGRES_URL",
    Engine.TRINO: "SQLANALYZER_E2E_TRINO_URL",
    Engine.SNOWFLAKE: "SQLANALYZER_E2E_SNOWFLAKE_URL",
}

# The same query for every engine: comma join, non-sargable cast and NOT IN - all fixable by the rule engine.
MESSY_SQL = (
    "SELECT o.id, c.name\n"
    "FROM orders o, customers c\n"
    "WHERE o.customer_id = c.id\n"
    "  AND CAST(o.ts AS DATE) = DATE '2024-01-15'\n"
    "  AND o.id NOT IN (SELECT r.order_id FROM refunds r)\n"
)
CLEAN_SQL = (
    "SELECT o.id, c.name\n"
    "FROM orders o JOIN customers c ON o.customer_id = c.id\n"
    "WHERE o.ts >= TIMESTAMP '2024-01-15 00:00:00' AND o.ts < TIMESTAMP '2024-01-16 00:00:00'\n"
    "  AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id)\n"
)
SCHEMA_TEXT = ("customers: id int, name text, region text\n"
               "orders: id int, customer_id int, ts timestamp, status text, amount int\n"
               "refunds: order_id int, amt int")


# ------------------------------------------------------------------ sample data
# The sample schema/data live in sqlanalyzer.demo (shared with the GUI's "Use demo database" and the scripts).
E2E_SIZES = {**SIZES, "order_items": 50_000, "shipments": 20_000}  # smaller side tables keep the suite fast


def load_duckdb(path: str) -> None:
    populate_duckdb(path, sizes=E2E_SIZES, replace=True)


def load_postgres(cfg: ConnectionConfig) -> None:
    populate_postgres(cfg, sizes=E2E_SIZES)


# ------------------------------------------------------------- PostgreSQL server
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TemporaryPostgres:
    """A throw-away cluster in a temp dir, started with the local binaries."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.port = _free_port()
        self.cfg = ConnectionConfig(Engine.POSTGRES, host="127.0.0.1", port=self.port, database="postgres",
                                    username="postgres")

    @staticmethod
    def available() -> bool:
        return shutil.which("initdb") is not None and shutil.which("pg_ctl") is not None

    def start(self) -> None:
        subprocess.run(["initdb", "-D", str(self.data), "-U", "postgres", "--auth=trust", "-E", "UTF8",
                        "--no-sync"], check=True, capture_output=True)
        options = f"-p {self.port} -k {self.root} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off"
        subprocess.run(["pg_ctl", "-D", str(self.data), "-o", options, "-l", str(self.root / "pg.log"), "-w",
                        "start"], check=True, capture_output=True)
        deadline = time.time() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                if time.time() > deadline:
                    raise RuntimeError("PostgreSQL did not come up") from None
                time.sleep(0.2)

    def stop(self) -> None:
        subprocess.run(["pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop"], capture_output=True)


@pytest.fixture(scope="session")
def pg_server(tmp_path_factory) -> Iterator[ConnectionConfig | None]:
    """Connection to a PostgreSQL with the sample data, or ``None`` when no server can be had."""
    url = os.environ.get(ENV_URLS[Engine.POSTGRES])
    if url:
        cfg = ConnectionConfig.from_url(Engine.POSTGRES, url)
        load_postgres(cfg)
        yield cfg
        drop_postgres(cfg)
        return
    if not TemporaryPostgres.available():
        yield None
        return
    server = TemporaryPostgres(tmp_path_factory.mktemp("pg"))
    try:
        server.start()
        load_postgres(server.cfg)
        yield server.cfg
    finally:
        server.stop()


@pytest.fixture(scope="session")
def duckdb_cfg(tmp_path_factory) -> ConnectionConfig:
    path = str(tmp_path_factory.mktemp("duck") / "e2e.duckdb")
    load_duckdb(path)
    return ConnectionConfig(Engine.DUCKDB, database=path)


# --------------------------------------------------------------- recorded servers
class RecordedServer:
    """Stands in for a database at the SQLAlchemy boundary.

    ``create_engine`` is replaced by :meth:`create_engine`; statements are recorded, ``SELECT 1`` succeeds,
    EXPLAIN statements return the fixture plan in the shape the real driver uses, anything mentioning
    ``missing_table`` fails like the server would.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.urls: list[str] = []
        self.statements: list[str] = []
        self.plan_text = {
            Engine.POSTGRES: (DATA / "postgres_plan.json").read_text(),
            Engine.SNOWFLAKE: (DATA / "snowflake_plan.json").read_text(),
            Engine.TRINO: (DATA / "trino_plan.txt").read_text(),
        }[engine]

    def create_engine(self, url, **_kwargs):
        self.urls.append(url.render_as_string(hide_password=False))
        return _FakeEngine(self)

    def rows(self, statement: str) -> list[tuple]:
        self.statements.append(statement)
        head = statement.strip().upper()
        if "MISSING_TABLE" in head:
            raise OperationalError(statement, {}, Exception(f"Table 'missing_table' does not exist ({self.engine})"))
        if head.startswith("SELECT 1"):
            return [(1,)]
        if head.startswith("EXPLAIN"):
            if self.engine is Engine.TRINO:
                return [(line,) for line in self.plan_text.splitlines()]
            if self.engine is Engine.POSTGRES:
                return [(json.loads(self.plan_text),)]
            return [(self.plan_text,)]
        raise OperationalError(statement, {}, Exception("statement not supported by the recorded server"))


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def scalar(self):
        return self._rows[0][0] if self._rows else None


class _FakeConnection:
    def __init__(self, server: RecordedServer):
        self._server = server

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, clause, *args, **kwargs):
        return _FakeResult(self._server.rows(getattr(clause, "text", str(clause))))

    def rollback(self):
        pass


class _FakeEngine:
    def __init__(self, server: RecordedServer):
        self._server = server

    def connect(self):
        return _FakeConnection(self._server)

    def begin(self):
        return _FakeConnection(self._server)

    def dispose(self):
        pass


# ----------------------------------------------------------------- engine matrix
@dataclass
class EngineEnv:
    engine: Engine
    cfg: ConnectionConfig
    live: bool
    recorded: RecordedServer | None = None
    queries: dict[str, str] = field(default_factory=dict)

    @property
    def label(self) -> str:
        return ENGINES[self.engine].label + (" (live)" if self.live else " (recorded server)")

    def explain_prefix(self, analyze: bool = False) -> str:
        return explain_statement(self.engine, "X", analyze).split(" X")[0]


def _recorded_config(engine: Engine) -> ConnectionConfig:
    return {
        Engine.POSTGRES: ConnectionConfig(engine, host="pg.example", database="shop", username="app", password="s3"),
        Engine.TRINO: ConnectionConfig(engine, host="trino.example", database="hive", schema="sales", username="app"),
        Engine.SNOWFLAKE: ConnectionConfig(engine, host="xy12345.eu-west-1", database="DB", schema="S",
                                           username="app", password="s3", extras={"warehouse": "WH"}),
    }[engine]


@pytest.fixture(params=list(Engine), ids=[e.value for e in Engine])
def engine_env(request, pg_server, duckdb_cfg, monkeypatch) -> EngineEnv:
    """One entry per engine: live when a server is available, recorded otherwise."""
    engine: Engine = request.param
    if engine is Engine.DUCKDB:
        return EngineEnv(engine, duckdb_cfg, True)
    if engine is Engine.POSTGRES:
        if pg_server is None:
            pytest.skip("no PostgreSQL: set SQLANALYZER_E2E_POSTGRES_URL or install initdb/pg_ctl")
        return EngineEnv(engine, pg_server, True)
    url = os.environ.get(ENV_URLS[engine])
    if url:
        return EngineEnv(engine, ConnectionConfig.from_url(engine, url), True)
    server = RecordedServer(engine)
    monkeypatch.setattr("sqlanalyzer.db.create_engine", server.create_engine)
    return EngineEnv(engine, _recorded_config(engine), False, server)


_BACKENDS = {"postgresql": Engine.POSTGRES, "trino": Engine.TRINO, "snowflake": Engine.SNOWFLAKE}


@pytest.fixture
def recorded(monkeypatch):
    """Factory for recorded servers; engines without one keep using the real ``create_engine`` (DuckDB)."""
    servers: dict[Engine, RecordedServer] = {}
    real_create_engine = create_engine

    def dispatch(url, **kwargs):
        engine = _BACKENDS.get(url.get_backend_name())
        if engine in servers:
            return servers[engine].create_engine(url, **kwargs)
        return real_create_engine(url, **kwargs)

    monkeypatch.setattr("sqlanalyzer.db.create_engine", dispatch)

    def factory(engine: Engine) -> RecordedServer:
        return servers.setdefault(engine, RecordedServer(engine))

    return factory
