"""Static detection of partition-pruning problems.

Partition columns come from (in priority order):
1. user-supplied hints: ``{"schema.table": ["dt", "region"]}`` (``"*"`` applies to every table)
2. a name heuristic (``dt``, ``event_date``, ``year`` ...) applied to columns the query references
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from sqlglot import exp

from .models import Category, Finding, Severity
from .sqlutil import (
    column_owner,
    comparison_sides,
    conjuncts,
    disjuncts,
    is_constant,
    is_wrapped,
    locate,
    real_tables,
    select_scopes,
    table_names,
)

PartitionHints = Mapping[str, Sequence[str]]

_HEURISTIC = re.compile(
    r"^(dt|ds|date_?key|part(ition)?(_?(date|dt|day|key|col))?|_?partition(_\w+)?|"
    r"(event|load|ingest(ion)?|business|process(ing)?|created|snapshot|report(ing)?)_(date|dt|day)|"
    r"year|month|day|hour|yyyymm(dd)?)$",
    re.IGNORECASE,
)
_PRUNING_OPS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In)


def parse_hints(text: str) -> dict[str, list[str]]:
    """Parse ``schema.table: dt, region`` lines (one table per line, ``#`` comments)."""
    hints: dict[str, list[str]] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        table, cols = line.split(":", 1)
        parsed = [c.strip().lower() for c in cols.split(",") if c.strip()]
        if parsed:
            hints[table.strip().lower()] = parsed
    return hints


def _hinted_columns(table: exp.Table, hints: PartitionHints) -> set[str]:
    bare, qualified = table_names(table)
    cols: set[str] = set()
    for key, value in hints.items():
        k = key.lower()
        if k in ("*", bare, qualified) or qualified.endswith("." + k):
            cols.update(c.lower() for c in value)
    return cols


def analyze_partitions(statements: list[exp.Expression], hints: PartitionHints | None = None) -> list[Finding]:
    hints = hints or {}
    out: list[Finding] = []
    for stmt in statements:
        for scope in select_scopes(stmt):
            out += _analyze_scope(scope, hints)
    return out


def _analyze_scope(scope, hints: PartitionHints) -> list[Finding]:
    sel: exp.Select = scope.expression
    tables = real_tables(scope)
    if not tables:
        return []

    preds = conjuncts(sel.args.get("where"))
    for j in sel.args.get("joins") or []:
        if j.args.get("on") is not None:
            preds += conjuncts(j.args["on"])

    referenced: dict[str, dict[str, exp.Column]] = {a: {} for a in tables}
    for col in sel.find_all(exp.Column):
        if col.find_ancestor(exp.Select) is not sel:
            continue
        owner = column_owner(col, tables)
        if owner:
            referenced[owner].setdefault(col.name.lower(), col)

    out: list[Finding] = []
    for alias, table in tables.items():
        _, qualified = table_names(table)
        configured = _hinted_columns(table, hints)
        heuristic = {c for c in referenced[alias] if _HEURISTIC.match(c)} if not configured else set()
        part_cols = configured | heuristic
        if not part_cols:
            continue
        origin = "configured" if configured else "likely"
        status = {c: _status(c, alias, preds, tables, sel) for c in part_cols}

        for col, (kind, node) in status.items():
            line, colno = locate(node) if node is not None else locate(table)
            if kind == "wrapped":
                out.append(Finding(
                    "partition-column-wrapped", Severity.WARNING, Category.PARTITION,
                    f"Predicate on {origin} partition column '{alias}.{col}' wraps it in a function/cast/arithmetic; partitions cannot be pruned.",
                    "Compare the bare column against constants, e.g. dt >= DATE '2024-01-01' AND dt < DATE '2024-02-01'.",
                    line, colno))
            elif kind == "leading-wildcard":
                out.append(Finding(
                    "partition-like-wildcard", Severity.WARNING, Category.PARTITION,
                    f"LIKE with leading wildcard on {origin} partition column '{alias}.{col}' cannot prune partitions.",
                    "Use equality, IN, or a range on the partition column.", line, colno))
            elif kind == "non-constant":
                out.append(Finding(
                    "partition-dynamic-filter", Severity.INFO, Category.PARTITION,
                    f"Partition column '{alias}.{col}' is filtered by a non-constant expression (column or subquery).",
                    "Pruning then depends on runtime/dynamic filtering. Where possible, supply a literal range as well.",
                    line, colno))
            elif kind == "or":
                out.append(Finding(
                    "partition-or-filter", Severity.WARNING, Category.PARTITION,
                    f"Filter on {origin} partition column '{alias}.{col}' is inside an OR with other columns; pruning is likely lost.",
                    "Restructure as UNION ALL, or AND the partition filter outside the OR.", line, colno))
            elif kind == "none" and configured:
                out.append(Finding(
                    "missing-partition-filter", Severity.WARNING, Category.PARTITION,
                    f"No filter on partition column(s) of '{qualified}' ({', '.join(sorted(configured))}); all partitions will be scanned.",
                    f"Add a predicate on '{col}'.", *locate(table)))
                break  # one finding per table is enough
            elif kind == "none" and not configured:
                out.append(Finding(
                    "possible-missing-partition-filter", Severity.INFO, Category.PARTITION,
                    f"'{alias}.{col}' looks like a partition column but is not filtered.",
                    "If the table is partitioned on it, add a predicate to enable pruning "
                    "(declare partition columns in the Partition hints box for precise checks).",
                    *locate(referenced[alias][col])))
    return out


def _status(col: str, alias: str, preds: list[exp.Expression], tables: dict[str, exp.Table], sel: exp.Select):
    """Classify how ``alias.col`` is constrained: ok|wrapped|non-constant|or|leading-wildcard|none."""
    best: tuple[str, exp.Expression | None] = ("none", None)
    order = {"none": 0, "non-constant": 1, "or": 2, "wrapped": 3, "leading-wildcard": 3, "ok": 4}

    def consider(kind: str, node: exp.Expression) -> None:
        nonlocal best
        if order[kind] > order[best[0]]:
            best = (kind, node)

    def mine(c: exp.Column) -> bool:
        return (c.name.lower() == col and column_owner(c, tables) == alias
                and c.find_ancestor(exp.Select) is sel)

    for p in preds:
        branches = disjuncts(p)
        if len(branches) > 1:
            touching = [b for b in branches if any(mine(c) for c in b.find_all(exp.Column))]
            if touching and len(touching) < len(branches) or (
                    touching and any(
                        any(not mine(c) for c in b.find_all(exp.Column)) for b in branches)):
                consider("or", p)
                continue
        for b in branches:
            _classify(b, mine, consider)
    return best


def _classify(p: exp.Expression, mine, consider) -> None:
    if isinstance(p, (exp.Like, exp.ILike)):
        if any(mine(c) for c in p.this.find_all(exp.Column)):
            pat = p.expression
            if isinstance(pat, exp.Literal) and pat.is_string and pat.this[:1] in ("%", "_"):
                consider("leading-wildcard", p)
            else:
                consider("ok", p)
        return
    if not isinstance(p, _PRUNING_OPS):
        return
    sides = comparison_sides(p)
    for s in sides:
        for c in s.find_all(exp.Column):
            if not mine(c):
                continue
            others = [o for o in sides if o is not s]
            if is_wrapped(c, p):
                consider("wrapped", p)
            elif all(is_constant(o) for o in others):
                consider("ok", p)
            else:
                consider("non-constant", p)
