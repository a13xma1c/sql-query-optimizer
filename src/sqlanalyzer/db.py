"""SQLAlchemy-backed connections used to fetch real EXPLAIN output."""

from __future__ import annotations

import json
import os
import struct
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import SQLAlchemyError

from .dialects import ENGINES, Engine, explain_statement
from .parsing import parse_sql


class ExplainError(RuntimeError):
    pass


# DuckDB 1.2+ writes new files in its *latest* storage format (version 999 in pre-releases / 2.x), which older
# DuckDB builds, the CLI, JDBC drivers and other tools cannot open. Every file this app creates is written in
# the v1.0.0 format instead (readable by DuckDB >= 1.0); existing files keep whatever version they have.
DUCKDB_STORAGE_VERSION = "v1.0.0"
_DUCKDB_STORAGE_VERSIONS = {64: "v1.0.0", 65: "v1.2.0", 66: "v1.3.0", 67: "v1.4.0", 999: "development/latest"}


def duckdb_storage_version(path: str | os.PathLike[str]) -> tuple[int, str] | None:
    """(storage version number, DuckDB release it corresponds to) of a database file, or None."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(64)
    except OSError:
        return None
    index = header.find(b"DUCK")
    if index < 0 or len(header) < index + 12:
        return None
    number = struct.unpack_from("<Q", header, index + 4)[0]
    return number, _DUCKDB_STORAGE_VERSIONS.get(number, f"newer than {max(_DUCKDB_STORAGE_VERSIONS) - 900}.x")


@dataclass
class ConnectionConfig:
    engine: Engine
    host: str = ""  # Snowflake: account identifier; DuckDB: unused
    port: int | None = None
    database: str = ""  # DuckDB: file path or ":memory:"; Trino: catalog; Snowflake: database
    schema: str = ""
    username: str = ""
    password: str = ""
    extras: dict[str, str] = field(default_factory=dict)  # warehouse, role, sslmode, ...

    def url(self) -> URL:
        info = ENGINES[self.engine]
        if self.engine is Engine.DUCKDB:
            query = {"storage_compatibility_version": DUCKDB_STORAGE_VERSION}
            query.update({k: v for k, v in self.extras.items() if v})
            return URL.create(info.sa_driver, database=self.database or ":memory:", query=query)
        database = self.database or None
        if self.engine in (Engine.TRINO, Engine.SNOWFLAKE) and self.schema and self.database:
            database = f"{self.database}/{self.schema}"
        query = {k: v for k, v in self.extras.items() if v}
        if self.engine is Engine.SNOWFLAKE:
            return URL.create(info.sa_driver, username=self.username or None, password=self.password or None,
                              host=self.host or None, database=database, query=query)
        return URL.create(info.sa_driver, username=self.username or None, password=self.password or None,
                          host=self.host or None, port=self.port or info.default_port, database=database,
                          query=query)

    def display(self) -> str:
        if self.engine is Engine.DUCKDB:
            return f"duckdb:///{self.database or ':memory:'}"  # the storage-format query string is an implementation detail
        return self.url().render_as_string(hide_password=True)

    @classmethod
    def from_url(cls, engine: Engine, url: str | URL) -> ConnectionConfig:
        """Inverse of :meth:`url` (``trino://u:p@host:8080/catalog/schema?key=value``)."""
        parsed = url if isinstance(url, URL) else make_url(url)
        database, schema = parsed.database or "", ""
        if engine in (Engine.TRINO, Engine.SNOWFLAKE) and "/" in database:
            database, schema = database.split("/", 1)
        return cls(engine, parsed.host or "", parsed.port, database, schema, parsed.username or "",
                   parsed.password or "", {k: str(v) for k, v in parsed.query.items()})


def is_read_only(sql: str, dialect: str) -> bool:
    """True if every statement is a plain query (safe to EXPLAIN ANALYZE)."""
    from sqlglot import exp

    parsed = parse_sql(sql, dialect)
    if not parsed.ok:
        return False
    return all(isinstance(s, (exp.Select, exp.Subquery, exp.SetOperation)) for s in parsed.statements)


def test_connection(cfg: ConnectionConfig) -> str:
    engine = create_engine(cfg.url())
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return "Connection successful."
    except SQLAlchemyError as exc:
        raise ExplainError(_short(exc)) from exc
    finally:
        engine.dispose()


def fetch_plan_text(cfg: ConnectionConfig, sql: str, analyze: bool = False) -> str:
    """Run EXPLAIN for ``sql`` and return the raw plan text/JSON."""
    dialect = ENGINES[cfg.engine].sqlglot_dialect
    if analyze and not is_read_only(sql, dialect):
        raise ExplainError("EXPLAIN ANALYZE executes the statement. Only plain SELECT queries are allowed.")
    stmt = explain_statement(cfg.engine, sql, analyze)
    engine = create_engine(cfg.url())
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(stmt)).fetchall()
            conn.rollback()
    except SQLAlchemyError as exc:
        raise ExplainError(_short(exc)) from exc
    finally:
        engine.dispose()
    return _rows_to_plan(cfg.engine, rows)


def _rows_to_plan(engine: Engine, rows: Sequence[Any]) -> str:
    if not rows:
        raise ExplainError("EXPLAIN returned no rows.")
    if engine is Engine.POSTGRES:
        value = rows[0][0]
        return value if isinstance(value, str) else json.dumps(value)
    if engine is Engine.DUCKDB:
        for row in rows:
            if len(row) > 1 and str(row[0]).endswith("plan"):
                return row[1]
        return rows[-1][-1]
    if engine is Engine.SNOWFLAKE:
        value = rows[0][0]
        return value if isinstance(value, str) else json.dumps(value)
    return "\n".join(str(r[0]) for r in rows)  # Trino: one text line per row


def release_duckdb() -> None:
    """Drop DuckDB's Python modules so its bindings can be torn down quietly at interpreter exit.

    DuckDB's nanobind bindings check for still-alive objects when their internals are destroyed.
    CPython clears module dictionaries during finalisation in an order that depends on the import
    order (which worker threads make non-deterministic), so the type constants of ``duckdb.sqltypes``
    are sometimes still referenced at that point and ``nanobind: leaked N instances`` is printed.
    Unloading the modules while the interpreter is fully alive makes shutdown deterministic. Call it
    from an ``atexit`` handler once no DuckDB connection is in use any more.
    """
    import gc

    names = [n for n in sys.modules if n == "duckdb" or n.startswith(("duckdb.", "duckdb_engine", "_duckdb"))]
    modules = [sys.modules.pop(n) for n in names]
    gc.collect()
    for module in modules:
        if not module.__name__.startswith("_duckdb"):
            try:
                vars(module).clear()
            except Exception:  # noqa: BLE001 - best effort, never fail at exit
                pass
    del modules
    gc.collect()


def _short(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    msg = str(orig or exc).strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__
