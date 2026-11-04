"""Schema hints: ``table: col type, col type`` lines -> SQLGlot schema mapping + table statistics.

The optimizer needs column -> table resolution to expand ``SELECT *`` and to run
SQLGlot's scope-aware passes; the offline cost model and the index advisor use
row counts and key information. Users declare tables in a small text format::

    orders (rows=2000000): id int pk, customer_id int, status varchar ndv=4, amount decimal(10, 2)
    sales.customers (rows=50000): id int unique, name text      # schema-qualified
    events: dt, user_id                                          # types and statistics are optional

Per table ``(rows=N)`` gives the row count; per column the flags ``pk`` / ``unique``
mark keys, ``indexed`` (synonyms ``sorted``, ``clustered``) says the engine can seek
or prune on the column, ``partitioned`` marks a partition/cluster key (it implies
``indexed`` and feeds the partition-pruning rules), and ``ndv=N`` is the number of
distinct values. :func:`format_schema` writes hints back in this format, which is
what the catalog introspection (:mod:`sqlanalyzer.catalog`) produces. Every table must use the
same qualification depth (``table`` or ``db.table`` or ``catalog.db.table``)
because SQLGlot's MappingSchema requires a uniform nesting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_UNKNOWN = "UNKNOWN"
_FLAGS = {"pk", "unique", "primary", "key"}
_INDEX_FLAGS = {"indexed", "index", "sorted", "clustered", "partitioned"}
_PARTITION_FLAGS = {"partitioned", "partition"}


@dataclass(frozen=True)
class TableStats:
    """What the cost model knows about a table (all optional)."""

    rows: int | None = None
    unique: frozenset[str] = frozenset()  # columns with unique values (keys)
    ndv: dict[str, int] = field(default_factory=dict, hash=False, compare=False)  # distinct values per column
    columns: tuple[str, ...] = ()
    indexed: frozenset[str] = frozenset()  # columns the engine can seek/prune on (index, sort key, partition)
    partition: frozenset[str] = frozenset()  # partition / clustering key columns
    primary: frozenset[str] = frozenset()  # primary-key columns (subset of ``unique``)

    def prunable(self, column: str) -> bool:
        column = column.lower()
        return column in self.indexed or column in self.unique


@dataclass
class SchemaHints:
    tables: dict[str, dict[str, str]] = field(default_factory=dict)  # "db.table" -> {col: type}
    stats: dict[str, TableStats] = field(default_factory=dict)  # "db.table" -> statistics
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.tables

    def stats_for(self, table: str) -> TableStats | None:
        table = table.lower()
        for name, stats in self.stats.items():
            if name == table or name.endswith("." + table) or table.endswith("." + name):
                return stats
        return None

    def mapping(self) -> dict[str, Any] | None:
        """Nested mapping accepted by ``sqlglot.optimizer.qualify`` (None if empty)."""
        if not self.tables:
            return None
        out: dict[str, Any] = {}
        for name, cols in self.tables.items():
            node = out
            parts = name.split(".")
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = dict(cols)
        return out

    def column_names(self, table: str) -> list[str]:
        table = table.lower()
        for name, cols in self.tables.items():
            if name == table or name.endswith("." + table):
                return list(cols)
        return []

    def partition_hints(self) -> dict[str, list[str]]:
        """``{table: [partition columns]}`` in the format :func:`sqlanalyzer.partition.analyze_partitions` takes."""
        return {name: sorted(stats.partition) for name, stats in self.stats.items() if stats.partition}

    def merged(self, other: SchemaHints) -> SchemaHints:
        """``other``'s tables override this object's (used when importing from a catalog)."""
        out = SchemaHints(dict(self.tables), dict(self.stats), list(self.errors))
        out.tables.update(other.tables)
        out.stats.update(other.stats)
        out.errors += [e for e in other.errors if e not in out.errors]
        return out


def _split_columns(text: str) -> list[str]:
    """Split on commas that are not inside parentheses (``decimal(10, 2)``)."""
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        if ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]


_NAME = re.compile(r'^("[^"]+"|[A-Za-z_][\w$]*)(\s+(.+))?$')
_TABLE = re.compile(r'^("?[\w$.]+"?)\s*(?:\((.*)\))?$')
_ROWS = re.compile(r"rows\s*=\s*([\d_]+)", re.I)
_NDV = re.compile(r"\bndv\s*=\s*([\d_]+)", re.I)


def parse_schema(text: str) -> SchemaHints:
    hints = SchemaHints()
    depths: set[int] = set()
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if ":" not in line:
            hints.errors.append(f"line {lineno}: expected 'table: column type, ...'")
            continue
        head, cols = line.split(":", 1)
        tm = _TABLE.match(head.strip())
        table = (tm.group(1) if tm else head).strip().strip('"').lower()
        if not tm or not table or not re.match(r"^[a-z_][\w$]*(\.[a-z_][\w$]*){0,2}$", table):
            hints.errors.append(f"line {lineno}: invalid table name {head.strip()!r}")
            continue
        rows: int | None = None
        if tm.group(2):
            rm = _ROWS.search(tm.group(2))
            if rm is None:
                hints.errors.append(f"line {lineno}: table options must look like '(rows=N)'")
            else:
                rows = int(rm.group(1).replace("_", ""))
        columns: dict[str, str] = {}
        unique: set[str] = set()
        primary: set[str] = set()
        indexed: set[str] = set()
        partition: set[str] = set()
        ndv: dict[str, int] = {}
        for col in _split_columns(cols):
            m = _NAME.match(col)
            if not m:
                hints.errors.append(f"line {lineno}: invalid column definition {col!r}")
                continue
            name = m.group(1).strip('"').lower()
            rest = (m.group(3) or "").strip()
            nm = _NDV.search(rest)
            if nm:
                ndv[name] = int(nm.group(1).replace("_", ""))
                rest = _NDV.sub("", rest).strip()
            words = rest.split()
            flags = {w.lower() for w in words if w.lower() in _FLAGS | _INDEX_FLAGS | _PARTITION_FLAGS}
            if flags & _FLAGS:
                unique.add(name)
                indexed.add(name)  # a key is always seekable
            if flags & {"pk", "primary"}:
                primary.add(name)
            if flags & _INDEX_FLAGS:
                indexed.add(name)
            if flags & _PARTITION_FLAGS:
                partition.add(name)
                indexed.add(name)
            words = [w for w in words if w.lower() not in _FLAGS | _INDEX_FLAGS | _PARTITION_FLAGS]
            columns[name] = (" ".join(words) or _UNKNOWN).upper()
        if not columns:
            hints.errors.append(f"line {lineno}: table {table!r} has no columns")
            continue
        depths.add(table.count("."))
        hints.tables[table] = columns
        hints.stats[table] = TableStats(rows, frozenset(unique), ndv, tuple(columns), frozenset(indexed),
                                        frozenset(partition), frozenset(primary))
    if len(depths) > 1:
        hints.errors.append("all tables must use the same qualification depth (e.g. all 'schema.table')")
        hints.tables.clear()
        hints.stats.clear()
    return hints


def format_schema(hints: SchemaHints) -> str:
    """Serialise hints in the text format :func:`parse_schema` reads (round-trips)."""
    lines = []
    for table, columns in hints.tables.items():
        stats = hints.stats.get(table, TableStats())
        head = table + (f" (rows={stats.rows})" if stats.rows is not None else "")
        parts = []
        for name, typ in columns.items():
            words = [name]
            if typ and typ != _UNKNOWN:
                words.append(typ.lower())
            if name in stats.primary:
                words.append("pk")
            elif name in stats.unique:
                words.append("unique")
            if name in stats.partition:
                words.append("partitioned")
            elif name in stats.indexed and name not in stats.unique:
                words.append("indexed")
            if name in stats.ndv:
                words.append(f"ndv={stats.ndv[name]}")
            parts.append(" ".join(words))
        lines.append(f"{head}: {', '.join(parts)}")
    return "\n".join(lines) + ("\n" if lines else "")
