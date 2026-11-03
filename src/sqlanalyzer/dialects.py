"""Supported SQL engines and how each maps onto SQLGlot / SQLAlchemy / EXPLAIN."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Engine(StrEnum):
    POSTGRES = "postgres"
    DUCKDB = "duckdb"
    TRINO = "trino"
    SNOWFLAKE = "snowflake"


@dataclass(frozen=True)
class EngineInfo:
    label: str
    sqlglot_dialect: str
    sa_driver: str
    default_port: int | None
    plan_format: str  # human description of the plan format we parse


ENGINES: dict[Engine, EngineInfo] = {
    Engine.POSTGRES: EngineInfo("PostgreSQL", "postgres", "postgresql+psycopg2", 5432, "EXPLAIN (FORMAT JSON)"),
    Engine.DUCKDB: EngineInfo("DuckDB", "duckdb", "duckdb", None, "EXPLAIN (FORMAT JSON)"),
    Engine.TRINO: EngineInfo("Trino", "trino", "trino", 8080, "EXPLAIN (TYPE DISTRIBUTED)"),
    Engine.SNOWFLAKE: EngineInfo("Snowflake", "snowflake", "snowflake", None, "EXPLAIN USING JSON"),
}


def explain_statement(engine: Engine, sql: str, analyze: bool = False) -> str:
    """Build the EXPLAIN statement for ``engine``.

    ``analyze`` executes the query where the engine supports it (PostgreSQL,
    DuckDB, Trino). Snowflake has no EXPLAIN ANALYZE; the flag is ignored.
    """
    sql = sql.strip().rstrip(";").strip()
    if engine is Engine.POSTGRES:
        opts = "ANALYZE, BUFFERS, FORMAT JSON" if analyze else "FORMAT JSON"
        return f"EXPLAIN ({opts}) {sql}"
    if engine is Engine.DUCKDB:
        opts = "ANALYZE, FORMAT JSON" if analyze else "FORMAT JSON"
        return f"EXPLAIN ({opts}) {sql}"
    if engine is Engine.TRINO:
        return f"EXPLAIN ANALYZE {sql}" if analyze else f"EXPLAIN (TYPE DISTRIBUTED) {sql}"
    return f"EXPLAIN USING JSON {sql}"
