"""Execution plan parsing, analysis, comparison and rendering."""

from __future__ import annotations

from ..dialects import Engine
from .duckdb import parse_duckdb
from .model import Plan, PlanNode, PlanParseError
from .postgres import parse_postgres
from .snowflake import parse_snowflake
from .trino import parse_trino


def parse_plan(engine: Engine, raw: str) -> Plan:
    parsers = {
        Engine.POSTGRES: parse_postgres,
        Engine.DUCKDB: parse_duckdb,
        Engine.SNOWFLAKE: parse_snowflake,
        Engine.TRINO: parse_trino,
    }
    try:
        return parsers[engine](raw)
    except PlanParseError:
        raise
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        raise PlanParseError(f"Could not parse {engine.value} plan: {exc}") from exc


__all__ = ["Plan", "PlanNode", "PlanParseError", "parse_plan"]
