"""Schema hints: ``table: col type, col type`` lines -> SQLGlot schema mapping.

The optimizer needs column -> table resolution to expand ``SELECT *`` and to run
SQLGlot's scope-aware passes. Users declare tables in a small text format::

    orders: id int, customer_id int, status varchar, amount decimal(10, 2), order_date date
    sales.customers: id int, name text      # schema-qualified
    events: dt, user_id                     # types are optional

Every table must use the same qualification depth (``table`` or ``db.table`` or
``catalog.db.table``) because SQLGlot's MappingSchema requires a uniform nesting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_UNKNOWN = "UNKNOWN"


@dataclass
class SchemaHints:
    tables: dict[str, dict[str, str]] = field(default_factory=dict)  # "db.table" -> {col: type}
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.tables

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


def _split_columns(text: str) -> list[str]:
    """Split on commas that are not inside parentheses (``decimal(10, 2)``)."""
    parts, depth, current = [], 0, []
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
        table, cols = line.split(":", 1)
        table = table.strip().strip('"').lower()
        if not table or not re.match(r"^[a-z_][\w$]*(\.[a-z_][\w$]*){0,2}$", table):
            hints.errors.append(f"line {lineno}: invalid table name {table!r}")
            continue
        columns: dict[str, str] = {}
        for col in _split_columns(cols):
            m = _NAME.match(col)
            if not m:
                hints.errors.append(f"line {lineno}: invalid column definition {col!r}")
                continue
            name = m.group(1).strip('"').lower()
            columns[name] = (m.group(3) or _UNKNOWN).strip().upper()
        if not columns:
            hints.errors.append(f"line {lineno}: table {table!r} has no columns")
            continue
        depths.add(table.count("."))
        hints.tables[table] = columns
    if len(depths) > 1:
        hints.errors.append("all tables must use the same qualification depth (e.g. all 'schema.table')")
        hints.tables.clear()
    return hints
