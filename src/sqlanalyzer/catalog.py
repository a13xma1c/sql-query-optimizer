"""Catalog introspection: read tables, columns, keys, indexes, statistics and partition keys from a database.

The result is a :class:`~sqlanalyzer.optimizer.schema.SchemaHints` - the same object
the *Schema* tab produces from text - so everything downstream (schema-aware
static rules, star expansion, the cost model, the index advisor, partition
pruning) works the same whether the schema was typed in or imported.

Per engine the sources are:

* **DuckDB** - ``duckdb_tables()`` (estimated size), ``duckdb_columns()``,
  ``duckdb_constraints()`` (PRIMARY KEY / UNIQUE), ``duckdb_indexes()``;
  distinct counts with ``approx_count_distinct`` on request.
* **PostgreSQL** - ``pg_class.reltuples``, ``pg_attribute``, ``pg_index`` (incl.
  primary/unique), ``pg_stats.n_distinct``, ``pg_partitioned_table``.
* **Trino** - ``information_schema.columns``, ``SHOW STATS FOR`` (row count,
  distinct values), ``SHOW COLUMNS`` / ``SHOW CREATE TABLE`` for partition keys.
* **Snowflake** - ``information_schema.tables`` (row count, clustering key),
  ``information_schema.columns``, ``SHOW PRIMARY KEYS`` / ``SHOW UNIQUE KEYS``;
  distinct counts with ``APPROX_COUNT_DISTINCT`` on request.

Queries are issued one at a time and every optional source is best effort:
a failing statistics query degrades the result instead of failing the import.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import partial

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from .db import ConnectionConfig, ExplainError
from .dialects import Engine
from .optimizer.schema import SchemaHints, TableStats

log = logging.getLogger(__name__)

Runner = Callable[[str], list[tuple]]  # executes SQL and returns rows


@dataclass
class CatalogOptions:
    tables: Sequence[str] | None = None  # restrict to these (bare or schema-qualified) names; None = every table
    schemas: Sequence[str] | None = None  # None = the connection's default schema
    distinct_counts: bool = False  # run approximate distinct counts (costs a scan per table)
    max_tables: int = 200
    max_distinct_columns: int = 12  # per table, when distinct_counts is on


@dataclass
class CatalogResult:
    hints: SchemaHints
    tables: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # optional sources that failed
    engine: Engine | None = None

    def summary(self) -> str:
        n = len(self.hints.tables)
        stats = sum(1 for s in self.hints.stats.values() if s.rows is not None)
        ndv = sum(len(s.ndv) for s in self.hints.stats.values())
        text_ = f"{n} table(s), {stats} with row counts, {ndv} column distinct count(s)"
        if self.skipped:
            text_ += f"; not available: {', '.join(self.skipped)}"
        return text_ + "."


# ------------------------------------------------------------------- entry point
def introspect(cfg: ConnectionConfig, options: CatalogOptions | None = None) -> CatalogResult:
    """Read the catalog of the database behind ``cfg``."""
    options = options or CatalogOptions()
    engine = create_engine(cfg.url())
    try:
        with engine.connect() as conn:
            def run(sql: str) -> list[tuple]:
                return [tuple(r) for r in conn.execute(text(sql)).fetchall()]

            return introspect_with(run, cfg.engine, options, default_schema=cfg.schema or None)
    except SQLAlchemyError as exc:
        raise ExplainError(_short(exc)) from exc
    finally:
        engine.dispose()


def introspect_with(run: Runner, engine: Engine, options: CatalogOptions | None = None,
                    default_schema: str | None = None) -> CatalogResult:
    """Same as :func:`introspect` but with an injected statement runner (testable without a server)."""
    options = options or CatalogOptions()
    reader = _READERS[engine](run, options, default_schema)
    hints, skipped = reader.read()
    return CatalogResult(hints, list(hints.tables), skipped, engine)


def _short(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    msg = str(orig or exc).strip()
    return msg.splitlines()[0] if msg else exc.__class__.__name__


# ------------------------------------------------------------------ collectors
class _Collector:
    """Accumulates per-table facts and turns them into SchemaHints."""

    def __init__(self) -> None:
        self.columns: dict[str, dict[str, str]] = {}
        self.rows: dict[str, int | None] = {}
        self.unique: dict[str, set[str]] = {}
        self.primary: dict[str, set[str]] = {}
        self.indexed: dict[str, set[str]] = {}
        self.partition: dict[str, set[str]] = {}
        self.ndv: dict[str, dict[str, int]] = {}

    def table(self, name: str) -> None:
        self.columns.setdefault(name.lower(), {})

    def column(self, table: str, column: str, typ: str) -> None:
        self.columns.setdefault(table.lower(), {})[column.lower()] = _normalize_type(typ)

    def key(self, table: str, columns: Sequence[str], primary: bool) -> None:
        table = table.lower()
        cols = [c.lower() for c in columns if c]
        if not cols:
            return
        if len(cols) == 1:
            self.unique.setdefault(table, set()).add(cols[0])
            if primary:
                self.primary.setdefault(table, set()).add(cols[0])
        self.indexed.setdefault(table, set()).add(cols[0])  # composite: the leading column is seekable

    def index(self, table: str, columns: Sequence[str], unique: bool) -> None:
        table = table.lower()
        cols = [c.lower() for c in columns if c]
        if not cols:
            return
        self.indexed.setdefault(table, set()).add(cols[0])
        if unique and len(cols) == 1:
            self.unique.setdefault(table, set()).add(cols[0])

    def hints(self, wanted: Iterable[str] | None, max_tables: int) -> SchemaHints:
        out = SchemaHints()
        wanted_set = {w.lower() for w in wanted} if wanted else None
        for table, columns in self.columns.items():
            if not columns:
                continue
            if wanted_set is not None and table not in wanted_set and table.split(".")[-1] not in wanted_set:
                continue
            if len(out.tables) >= max_tables:
                break
            out.tables[table] = dict(columns)
            out.stats[table] = TableStats(
                self.rows.get(table), frozenset(self.unique.get(table, ())), dict(self.ndv.get(table, {})),
                tuple(columns), frozenset(self.indexed.get(table, set()) | self.partition.get(table, set())),
                frozenset(self.partition.get(table, ())), frozenset(self.primary.get(table, ())))
        return out


def _normalize_type(typ: str) -> str:
    typ = (typ or "").strip().upper()
    return typ or "UNKNOWN"


class _Reader:
    def __init__(self, run: Runner, options: CatalogOptions, default_schema: str | None) -> None:
        self.run = run
        self.options = options
        self.default_schema = default_schema
        self.collector = _Collector()
        self.skipped: list[str] = []

    def optional(self, label: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - optional sources must not break the import
            log.debug("catalog source %s failed: %s", label, exc)
            self.skipped.append(label)

    def read(self) -> tuple[SchemaHints, list[str]]:
        raise NotImplementedError

    def qualified(self, schema: str | None, table: str) -> str:
        if schema and self.options.schemas:
            return f"{schema}.{table}".lower()
        return table.lower()

    def finish(self) -> tuple[SchemaHints, list[str]]:
        if self.options.distinct_counts:
            self.optional("distinct counts", self.distinct_counts)
        return self.collector.hints(self.options.tables, self.options.max_tables), self.skipped

    def distinct_counts(self) -> None:
        wanted = {w.lower() for w in self.options.tables} if self.options.tables else None
        for table, columns in list(self.collector.columns.items()):
            if wanted is not None and table not in wanted and table.split(".")[-1] not in wanted:
                continue
            cols = [c for c in columns if c not in self.collector.unique.get(table, ())][: self.options.max_distinct_columns]
            if not cols:
                continue
            exprs = ", ".join(f"{self.approx_distinct}({self.quote(c)})" for c in cols)
            rows = self.run(f"SELECT {exprs} FROM {self.quote_table(table)}")
            if rows:
                for col, value in zip(cols, rows[0], strict=True):
                    if value is not None and int(value) > 0:
                        self.collector.ndv.setdefault(table, {})[col] = int(value)

    approx_distinct = "APPROX_COUNT_DISTINCT"

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def quote_table(self, table: str) -> str:
        return ".".join(self.quote(p) for p in table.split("."))


# --------------------------------------------------------------------- DuckDB
class _DuckDB(_Reader):
    def read(self) -> tuple[SchemaHints, list[str]]:
        schemas = self._schemas()
        where = " OR ".join(f"schema_name = '{s}'" for s in schemas)
        for schema, table, size in self.run(f"SELECT schema_name, table_name, estimated_size FROM duckdb_tables() "
                                            f"WHERE NOT internal AND ({where})"):
            name = self.qualified(schema, table)
            self.collector.table(name)
            self.collector.rows[name] = int(size) if size is not None else None
        for schema, table in self.run(f"SELECT schema_name, view_name FROM duckdb_views() WHERE NOT internal AND ({where})"):
            self.collector.table(self.qualified(schema, table))
        for schema, table, column, typ in self.run(
                f"SELECT schema_name, table_name, column_name, data_type FROM duckdb_columns() "
                f"WHERE NOT internal AND ({where}) ORDER BY schema_name, table_name, column_index"):
            self.collector.column(self.qualified(schema, table), column, typ)
        self.optional("constraints", self._constraints)
        self.optional("indexes", self._indexes)
        return self.finish()

    def _schemas(self) -> list[str]:
        if self.options.schemas:
            return list(self.options.schemas)
        rows = self.run("SELECT current_schema()")
        return [rows[0][0] if rows else "main"]

    def _constraints(self) -> None:
        for schema, table, kind, cols in self.run(
                "SELECT schema_name, table_name, constraint_type, constraint_column_names FROM duckdb_constraints()"):
            if kind in ("PRIMARY KEY", "UNIQUE"):
                self.collector.key(self.qualified(schema, table), list(cols or []), kind == "PRIMARY KEY")

    def _indexes(self) -> None:
        for schema, table, unique, expressions in self.run(
                "SELECT schema_name, table_name, is_unique, expressions FROM duckdb_indexes()"):
            cols = [c.strip() for c in str(expressions or "").strip("[]").split(",") if c.strip()]
            cols = [c for c in cols if re.fullmatch(r"[\w$]+", c)]  # skip expression indexes
            self.collector.index(self.qualified(schema, table), cols, bool(unique))

    approx_distinct = "approx_count_distinct"


# ----------------------------------------------------------------- PostgreSQL
class _Postgres(_Reader):
    def read(self) -> tuple[SchemaHints, list[str]]:
        schemas = self._schemas()
        in_list = ", ".join(f"'{s}'" for s in schemas)
        for schema, table, tuples in self.run(
                "SELECT n.nspname, c.relname, c.reltuples FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE c.relkind IN ('r', 'p', 'm', 'v') AND n.nspname IN ({in_list}) ORDER BY 1, 2"):
            name = self.qualified(schema, table)
            self.collector.table(name)
            self.collector.rows[name] = int(tuples) if tuples is not None and float(tuples) >= 0 else None
        for schema, table, column, typ in self.run(
                "SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod) FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE a.attnum > 0 AND NOT a.attisdropped AND c.relkind IN ('r', 'p', 'm', 'v') "
                f"AND n.nspname IN ({in_list}) ORDER BY 1, 2, a.attnum"):
            self.collector.column(self.qualified(schema, table), column, typ)
        self.optional("indexes", lambda: self._indexes(in_list))
        self.optional("statistics", lambda: self._statistics(in_list))
        self.optional("partitions", lambda: self._partitions(in_list))
        return self.finish()

    def _schemas(self) -> list[str]:
        if self.options.schemas:
            return list(self.options.schemas)
        rows = self.run("SELECT current_schema()")
        return [rows[0][0] if rows and rows[0][0] else "public"]

    def _indexes(self, in_list: str) -> None:
        for schema, table, unique, primary, cols in self.run(
                "SELECT n.nspname, t.relname, ix.indisunique, ix.indisprimary, "
                "array_agg(a.attname ORDER BY k.ord) FILTER (WHERE a.attname IS NOT NULL) "
                "FROM pg_index ix JOIN pg_class t ON t.oid = ix.indrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
                "CROSS JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) "
                "LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
                f"WHERE n.nspname IN ({in_list}) GROUP BY 1, 2, 3, 4, ix.indexrelid"):
            cols = list(cols or [])
            if primary or (unique and len(cols) == 1):
                self.collector.key(self.qualified(schema, table), cols, bool(primary))
            else:
                self.collector.index(self.qualified(schema, table), cols, bool(unique))

    def _statistics(self, in_list: str) -> None:
        for schema, table, column, n_distinct in self.run(
                f"SELECT schemaname, tablename, attname, n_distinct FROM pg_stats WHERE schemaname IN ({in_list})"):
            name = self.qualified(schema, table)
            if n_distinct is None or name not in self.collector.columns:
                continue
            value = float(n_distinct)
            rows = self.collector.rows.get(name)
            if value < 0 and rows:
                value = -value * rows  # negative = fraction of the rows
            if value >= 1:
                self.collector.ndv.setdefault(name, {})[column.lower()] = int(value)

    def _partitions(self, in_list: str) -> None:
        for schema, table, cols in self.run(
                "SELECT n.nspname, c.relname, array_agg(a.attname ORDER BY k.ord) FROM pg_partitioned_table p "
                "JOIN pg_class c ON c.oid = p.partrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL unnest(p.partattrs) WITH ORDINALITY AS k(attnum, ord) "
                "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.attnum "
                f"WHERE n.nspname IN ({in_list}) GROUP BY 1, 2"):
            name = self.qualified(schema, table)
            self.collector.partition.setdefault(name, set()).update(c.lower() for c in cols or [])

    def distinct_counts(self) -> None:  # pg_stats already provides them; exact counts are too expensive
        return


# ---------------------------------------------------------------------- Trino
class _Trino(_Reader):
    def read(self) -> tuple[SchemaHints, list[str]]:
        schemas = self._schemas()
        in_list = ", ".join(f"'{s}'" for s in schemas)
        for schema, table, column, typ in self.run(
                "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                f"WHERE table_schema IN ({in_list}) ORDER BY table_schema, table_name, ordinal_position"):
            self.collector.column(self.qualified(schema, table), column, typ)
        wanted = {w.lower() for w in self.options.tables} if self.options.tables else None
        for name in list(self.collector.columns):
            if wanted is not None and name not in wanted and name.split(".")[-1] not in wanted:
                continue
            self.optional(f"stats for {name}", partial(self._stats, name))
            self.optional(f"partitioning of {name}", partial(self._partitioning, name))
        return self.finish()

    def _schemas(self) -> list[str]:
        if self.options.schemas:
            return list(self.options.schemas)
        if self.default_schema:
            return [self.default_schema]
        rows = self.run("SELECT current_schema")
        return [rows[0][0]] if rows and rows[0][0] else []

    def _stats(self, table: str) -> None:
        rows = self.run(f"SHOW STATS FOR {self.quote_table(table)}")
        for row in rows:
            column, distinct, row_count = row[0], row[2], row[4] if len(row) > 4 else None
            if column is None and row_count is not None:
                self.collector.rows[table] = int(row_count)
            elif column is not None and distinct is not None:
                self.collector.ndv.setdefault(table, {})[str(column).lower()] = int(distinct)

    def _partitioning(self, table: str) -> None:
        for row in self.run(f"SHOW COLUMNS FROM {self.quote_table(table)}"):
            extra = str(row[2] or "") if len(row) > 2 else ""
            if "partition key" in extra.lower():
                self.collector.partition.setdefault(table, set()).add(str(row[0]).lower())
        if table not in self.collector.partition:
            ddl = self.run(f"SHOW CREATE TABLE {self.quote_table(table)}")
            match = re.search(r"partitioning\s*=\s*ARRAY\[(.*?)\]", str(ddl[0][0]) if ddl else "", re.I | re.S)
            if match:
                for item in re.findall(r"'([^']+)'", match.group(1)):
                    column = re.sub(r"^\w+\((\w+).*\)$", r"\1", item.strip())  # day(ts) -> ts
                    self.collector.partition.setdefault(table, set()).add(column.lower())

    approx_distinct = "approx_distinct"


# ------------------------------------------------------------------ Snowflake
class _Snowflake(_Reader):
    def read(self) -> tuple[SchemaHints, list[str]]:
        schemas = self._schemas()
        in_list = ", ".join(f"'{s.upper()}'" for s in schemas)
        for schema, table, rows, clustering in self.run(
                "SELECT table_schema, table_name, row_count, clustering_key FROM information_schema.tables "
                f"WHERE table_schema IN ({in_list}) ORDER BY 1, 2"):
            name = self.qualified(schema, table)
            self.collector.table(name)
            self.collector.rows[name] = int(rows) if rows is not None else None
            for column in _clustering_columns(clustering):
                self.collector.partition.setdefault(name, set()).add(column)
        for schema, table, column, typ in self.run(
                "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns "
                f"WHERE table_schema IN ({in_list}) ORDER BY table_schema, table_name, ordinal_position"):
            self.collector.column(self.qualified(schema, table), column, typ)
        self.optional("primary keys", lambda: self._keys("PRIMARY", True))
        self.optional("unique keys", lambda: self._keys("UNIQUE", False))
        return self.finish()

    def _schemas(self) -> list[str]:
        if self.options.schemas:
            return list(self.options.schemas)
        if self.default_schema:
            return [self.default_schema]
        rows = self.run("SELECT current_schema()")
        return [rows[0][0]] if rows and rows[0][0] else ["PUBLIC"]

    def _keys(self, kind: str, primary: bool) -> None:
        rows = self.run(f"SHOW {kind} KEYS IN SCHEMA")
        by_constraint: dict[tuple[str, str], list[tuple[int, str]]] = {}
        for row in rows:
            # created_on, database_name, schema_name, table_name, column_name, key_sequence, constraint_name, ...
            schema, table, column, seq, constraint = row[2], row[3], row[4], row[5], row[6]
            by_constraint.setdefault((self.qualified(schema, table), str(constraint)), []).append((int(seq), column))
        for (table, _), cols in by_constraint.items():
            self.collector.key(table, [c for _, c in sorted(cols)], primary)


def _clustering_columns(spec) -> list[str]:
    if not spec:
        return []
    inner = re.sub(r"^\s*LINEAR\s*\((.*)\)\s*$", r"\1", str(spec), flags=re.I | re.S)
    out = []
    for item in inner.split(","):
        item = re.sub(r"^\w+\((\w+).*\)$", r"\1", item.strip())  # TO_DATE(ts) -> ts
        item = item.strip().strip('"').lower()
        if re.fullmatch(r"[\w$]+", item):
            out.append(item)
    return out


_READERS: dict[Engine, type[_Reader]] = {
    Engine.DUCKDB: _DuckDB,
    Engine.POSTGRES: _Postgres,
    Engine.TRINO: _Trino,
    Engine.SNOWFLAKE: _Snowflake,
}


__all__ = ["CatalogOptions", "CatalogResult", "introspect", "introspect_with"]
