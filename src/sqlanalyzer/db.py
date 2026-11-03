"""SQLAlchemy-backed connections used to fetch real EXPLAIN output."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError

from .dialects import ENGINES, Engine, explain_statement
from .parsing import parse_sql


class ExplainError(RuntimeError):
    pass


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
            return URL.create(info.sa_driver, database=self.database or ":memory:")
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
        return self.url().render_as_string(hide_password=True)


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


def _rows_to_plan(engine: Engine, rows: list) -> str:
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


def _short(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    msg = str(orig or exc).strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__
