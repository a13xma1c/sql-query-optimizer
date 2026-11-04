"""Schema-aware static rules: what can only be checked when the tables are known.

With a declared or imported schema (types, keys, sizes) the analyzer can verify
what the heuristic rules in :mod:`sqlanalyzer.static` have to guess:

* tables and columns that do not exist, unqualified columns that exist in more
  than one joined table;
* join keys of different type families (``int = varchar`` forces an implicit
  cast on every row and usually disables hash-join key matching / index use);
* predicates comparing a column with a literal of another family
  (``amount = '5'``, ``order_date = 20240101``);
* joins on non-key, non-indexed columns of large tables;
* how many columns ``SELECT *`` really reads.

Findings use the ``schema`` category.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from .models import Category, Finding, Severity
from .optimizer.schema import TableStats
from .sqlutil import column_owner, conjuncts, locate, real_tables, select_of, select_scopes, table_names

WIDE_STAR = 20
LARGE_TABLE = 500_000

_FAMILIES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\[\]$|^(JSON|JSONB|VARIANT|OBJECT|ARRAY|STRUCT|MAP|LIST|ROW|SUPER)(?!\w)"), "complex"),
    (re.compile(r"^(BOOL|BOOLEAN)(?!\w)"), "bool"),
    (re.compile(r"^(TINYINT|SMALLINT|INT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|UINTEGER|UBIGINT|SERIAL|"
                r"BIGSERIAL|SMALLSERIAL|INT2|INT4|INT8)(?!\w)|^NUMBER\(\d+(,\s*0)?\)"), "integer"),
    (re.compile(r"^(DECIMAL|NUMERIC|NUMBER|MONEY)(?!\w)"), "decimal"),
    (re.compile(r"^(FLOAT|DOUBLE|REAL|FLOAT4|FLOAT8|DOUBLE PRECISION)(?!\w)"), "float"),
    (re.compile(r"^(TIMESTAMP|DATETIME|TIMESTAMPTZ|TIMESTAMP_NTZ|TIMESTAMP_LTZ|TIMESTAMP_TZ)(?!\w)"), "timestamp"),
    (re.compile(r"^DATE(?!\w)"), "date"),
    (re.compile(r"^(TIME|TIMETZ)(?!\w)"), "time"),
    (re.compile(r"^(VARCHAR|TEXT|CHAR|CHARACTER|STRING|NVARCHAR|NCHAR|BPCHAR|CHARACTER VARYING|CITEXT|ENUM)(?!\w)"), "text"),
    (re.compile(r"^(UUID)(?!\w)"), "uuid"),
    (re.compile(r"^(BLOB|BYTEA|BINARY|VARBINARY)(?!\w)"), "binary"),
]
_NUMERIC = {"integer", "decimal", "float"}
_TEMPORAL = {"date", "timestamp"}
_DATE_LITERAL = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")


def type_family(typ: str | None) -> str | None:
    """Coarse family of a SQL type name (``None`` when unknown)."""
    if not typ:
        return None
    upper = typ.strip().upper()
    if upper in ("", "UNKNOWN"):
        return None
    for pattern, family in _FAMILIES:
        if pattern.search(upper):
            return family
    return "other"


def compatible(a: str | None, b: str | None) -> str:
    """``"ok"``, ``"soft"`` (implicit but cheap, e.g. date vs timestamp) or ``"mismatch"``."""
    if a is None or b is None or a == b or a == "other" or b == "other":
        return "ok"
    if a in _NUMERIC and b in _NUMERIC:
        return "soft"
    if a in _TEMPORAL and b in _TEMPORAL:
        return "soft"
    return "mismatch"


# ------------------------------------------------------------------ schema index
@dataclass
class SchemaIndex:
    """Case-insensitive lookups over a ``{table: {column: type}}`` (possibly nested) mapping."""

    tables: dict[str, dict[str, str]]
    stats: Mapping[str, TableStats]

    @classmethod
    def build(cls, schema: Mapping[str, Any] | None, stats: Mapping[str, TableStats] | None = None) -> SchemaIndex:
        flat: dict[str, dict[str, str]] = {}

        def walk(node: Mapping[str, Any], prefix: str) -> None:
            for key, value in node.items():
                name = f"{prefix}{key}".lower()
                if isinstance(value, Mapping) and value and all(isinstance(v, Mapping) for v in value.values()):
                    walk(value, f"{name}.")
                elif isinstance(value, Mapping):
                    flat[name] = {str(c).lower(): str(t) for c, t in value.items()}

        if schema:
            walk(schema, "")
        return cls(flat, {k.lower(): v for k, v in (stats or {}).items()})

    @property
    def empty(self) -> bool:
        return not self.tables

    def lookup(self, table: exp.Table) -> tuple[str, dict[str, str]] | None:
        bare, qualified = table_names(table)
        for key in (qualified, bare):
            if key in self.tables:
                return key, self.tables[key]
        matches = [k for k in self.tables if k.split(".")[-1] == bare]
        if len(matches) == 1:
            return matches[0], self.tables[matches[0]]
        return None

    def stats_for(self, key: str) -> TableStats | None:
        if key in self.stats:
            return self.stats[key]
        bare = key.split(".")[-1]
        return next((s for k, s in self.stats.items() if k.split(".")[-1] == bare), None)


# ---------------------------------------------------------------------- rules
def _f(rule: str, sev: Severity, node: exp.Expr | None, msg: str, tip: str) -> Finding:
    line, col = locate(node) if node is not None else (None, None)
    return Finding(rule, sev, Category.SCHEMA, msg, tip, line, col)


def analyze_schema_aware(statements: list[exp.Expr], schema: Mapping[str, Any] | None,
                         stats: Mapping[str, TableStats] | None = None) -> list[Finding]:
    index = SchemaIndex.build(schema, stats)
    if index.empty:
        return []
    out: list[Finding] = []
    for stmt in statements:
        ctes = {c.alias.lower() for c in stmt.find_all(exp.CTE)}
        for scope in select_scopes(stmt):
            out += _check_scope(scope, index, ctes)
    return out


def _check_scope(scope, index: SchemaIndex, ctes: set[str]) -> list[Finding]:
    sel = select_of(scope)
    tables = real_tables(scope)
    out: list[Finding] = []
    known: dict[str, tuple[str, dict[str, str]]] = {}  # alias -> (schema key, columns)
    for alias, table in tables.items():
        if table.name.lower() in ctes and not table.db:
            continue
        found = index.lookup(table)
        if found is None:
            _, qualified = table_names(table)
            out.append(_f("unknown-table", Severity.WARNING, table,
                          f"Table '{qualified}' is not in the declared schema.",
                          "Check the name, or import/declare the table in the Schema tab so its columns, keys and "
                          "statistics can be used."))
        else:
            known[alias] = found

    # -- columns
    sources = set(scope.sources) | set(tables)
    for column in sel.find_all(exp.Column):
        if column.find_ancestor(exp.Select) is not sel or isinstance(column.this, exp.Star):
            continue
        name = column.name.lower()
        if column.table:
            entry = known.get(column.table)
            if column.table not in sources and not _outer_alias(column.table, scope):
                out.append(_f("unknown-alias", Severity.WARNING, column,
                              f"'{column.table}' is not a table or alias of this query block.",
                              "Check the alias; the engine will reject the query."))
            elif entry is not None and name not in entry[1]:
                out.append(_f("unknown-column", Severity.WARNING, column,
                              f"Column '{column.sql()}' does not exist in '{entry[0]}' "
                              f"({len(entry[1])} columns declared).",
                              "Check the column name or refresh the schema."))
        elif len(tables) > 1 and known and len(known) == len(tables):
            owners = [alias for alias, (_, cols) in known.items() if name in cols]
            if len(owners) > 1:
                out.append(_f("ambiguous-column", Severity.WARNING, column,
                              f"Unqualified column '{name}' exists in {', '.join(owners)}.",
                              "Qualify it with the table alias; engines reject or silently pick one."))
            elif not owners and not _is_alias_reference(column, sel):
                out.append(_f("unknown-column", Severity.WARNING, column,
                              f"Column '{name}' is not in any of the joined tables ({', '.join(known)}).",
                              "Check the column name or refresh the schema."))

    # -- SELECT *
    for star in sel.find_all(exp.Star):
        parent = star.parent
        star_sources: list[str]
        if isinstance(parent, exp.Column) and parent.table in known:
            width = len(known[parent.table][1])
            star_sources = [parent.table]
        elif isinstance(parent, exp.Select) and parent is sel and known and len(known) == len(tables):
            width = sum(len(cols) for _, cols in known.values())
            star_sources = list(known)
        else:
            continue
        if width >= WIDE_STAR:
            out.append(_f("wide-select-star", Severity.WARNING, star,
                          f"SELECT * reads {width} columns from {', '.join(star_sources)}.",
                          "List the columns you need; on columnar engines every extra column is extra I/O."))

    # -- join keys and predicates
    def family_of(column: exp.Column) -> tuple[str | None, str | None]:
        alias = column_owner(column, tables)
        if alias is None or alias not in known:
            return None, None
        typ = known[alias][1].get(column.name.lower())
        return type_family(typ), typ

    preds = conjuncts(sel.args.get("where"))
    for join in sel.args.get("joins") or []:
        if join.args.get("on") is not None:
            preds += conjuncts(join.args["on"])
    for pred in preds:
        out += _check_predicate(pred, family_of, tables, known, index)
    return out


def _outer_alias(alias: str, scope) -> bool:
    """True if ``alias`` is a source of an enclosing query block (correlated reference)."""
    parent = scope.parent
    while parent is not None:
        if alias in parent.sources:
            return True
        parent = parent.parent
    return False


def _is_alias_reference(column: exp.Column, sel: exp.Select) -> bool:
    aliases = {e.alias.lower() for e in sel.expressions if isinstance(e, exp.Alias) and e.alias}
    return column.name.lower() in aliases


def _check_predicate(pred: exp.Expr, family_of, tables, known, index: SchemaIndex) -> list[Finding]:
    while isinstance(pred, exp.Paren):
        pred = pred.this
    out: list[Finding] = []
    if not isinstance(pred, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)):
        return out
    left = pred.this
    candidates = list(pred.expressions) if isinstance(pred, exp.In) else (
        [pred.args.get("low"), pred.args.get("high")] if isinstance(pred, exp.Between) else [pred.expression])
    rights: list[exp.Expr] = [r for r in candidates if r is not None]
    if not isinstance(left, exp.Column):
        return out
    left_family, left_type = family_of(left)
    if left_family is None:
        return out
    for right in rights:
        if isinstance(right, exp.Column):
            right_family, right_type = family_of(right)
            if right_family is None:
                continue
            verdict = compatible(left_family, right_family)
            if column_owner(left, tables) != column_owner(right, tables):
                if verdict == "mismatch":
                    out.append(_f("join-type-mismatch", Severity.WARNING, pred,
                                  f"Join key types differ: {left.sql()} is {left_type}, {right.sql()} is {right_type}.",
                                  "The engine casts one side for every row; indexes, zone maps and hash-join key "
                                  "matching on that side are lost. Align the column types or cast the smaller side "
                                  "once in a subquery."))
                elif verdict == "soft":
                    out.append(_f("join-type-mismatch", Severity.INFO, pred,
                                  f"Join keys have related but different types: {left.sql()} is {left_type}, "
                                  f"{right.sql()} is {right_type}.",
                                  "Implicit widening works, but identical types keep join planning simplest."))
                elif isinstance(pred, exp.EQ):
                    out += _non_key_join(left, right, tables, known, index, pred)
            continue
        literal_family = _literal_family(right)
        if literal_family is None:
            continue
        if literal_family == "datestring":
            if left_family in _TEMPORAL or left_family == "text":
                continue
            literal_family = "text"
        verdict = compatible(left_family, literal_family)
        if verdict == "mismatch":
            out.append(_f("predicate-type-mismatch", Severity.WARNING, pred,
                          f"'{left.sql()}' is {left_type} but is compared with a {literal_family} literal "
                          f"({right.sql()}).",
                          "Use a literal of the column's type; an implicit cast of the column defeats indexes "
                          "and can change the comparison semantics."))
    return out


def _non_key_join(left: exp.Column, right: exp.Column, tables, known, index: SchemaIndex,
                  pred: exp.Expr) -> list[Finding]:
    out = []
    for column in (left, right):
        alias = column_owner(column, tables)
        if alias is None or alias not in known:
            continue
        stats = index.stats_for(known[alias][0])
        if stats is None or not stats.rows or stats.rows < LARGE_TABLE:
            continue
        if not stats.prunable(column.name):
            out.append(_f("join-on-unindexed-column", Severity.INFO, pred,
                          f"Join key '{column.sql()}' is neither a key nor indexed on a table of ~{stats.rows:,} rows.",
                          "See the Tuning tab for an index/sort-order recommendation on the join column."))
    return out


def _literal_family(node: exp.Expr) -> str | None:
    while isinstance(node, (exp.Paren, exp.Neg)):
        node = node.this
    if isinstance(node, exp.Cast):
        return type_family(node.to.sql())
    if isinstance(node, exp.Literal):
        if node.is_string:
            return "datestring" if _DATE_LITERAL.match(node.this.strip()) else "text"
        return "integer" if re.fullmatch(r"-?\d+", node.this) else "decimal"
    if isinstance(node, exp.Boolean):
        return "bool"
    return None


__all__ = ["SchemaIndex", "analyze_schema_aware", "compatible", "type_family"]
