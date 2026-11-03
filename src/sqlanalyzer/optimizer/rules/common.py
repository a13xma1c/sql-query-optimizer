"""Helpers shared by the rewrite rules."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterable

from sqlglot import exp

from ...sqlutil import conjuncts

COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
NULL_REJECTING = (*COMPARISONS, exp.Between, exp.In, exp.Like, exp.ILike)
_STRICT_WRAPPERS = (exp.Cast, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Lower, exp.Upper, exp.Trim, exp.Substring,
                    exp.Length, exp.Date, exp.Year, exp.Month, exp.Day, exp.DateTrunc, exp.Extract, exp.Paren, exp.Neg)


def is_literal_constant(node: exp.Expression) -> bool:
    """Literal / boolean / NULL, optionally under CAST, DATE '...' or a unary minus."""
    while isinstance(node, (exp.Cast, exp.Paren, exp.Neg)):
        node = node.this
    return isinstance(node, (exp.Literal, exp.Boolean, exp.Null))


def literal_string(node: exp.Expression) -> str | None:
    """The string of a string literal (possibly wrapped in CAST/DATE), else None."""
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    return None


def literal_int(node: exp.Expression) -> int | None:
    while isinstance(node, (exp.Cast, exp.Paren)):
        node = node.this
    neg = False
    if isinstance(node, exp.Neg):
        neg, node = True, node.this
    if isinstance(node, exp.Literal) and not node.is_string:
        try:
            value = int(node.this)
        except ValueError:
            return None
        return -value if neg else value
    return None


def parse_date(text: str) -> dt.date | None:
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y%m%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def date_literal(value: dt.date) -> exp.Expression:
    return exp.cast(exp.Literal.string(value.isoformat()), exp.DataType.Type.DATE)


def set_where(select: exp.Select, predicates: Iterable[exp.Expression]) -> None:
    preds = [p for p in predicates if p is not None]
    if preds:
        select.set("where", exp.Where(this=exp.and_(*preds, copy=False)))
    else:
        select.set("where", None)


def where_conjuncts(select: exp.Select) -> list[exp.Expression]:
    return conjuncts(select.args.get("where"))


def column_tables(node: exp.Expression) -> set[str]:
    return {c.table for c in node.find_all(exp.Column) if c.table}


def is_null_rejecting(pred: exp.Expression, alias: str) -> bool:
    """True if ``pred`` is false/unknown whenever every ``alias`` column is NULL.

    Conservative: the alias column must sit directly under a comparison (possibly
    inside strict functions such as CAST or arithmetic) and not under COALESCE,
    CASE, IS NULL, OR, ... .
    """
    if isinstance(pred, exp.Not):
        inner = pred.this
        return isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null) and _direct_column(inner.this, alias)
    if isinstance(pred, NULL_REJECTING):
        sides = [pred.this]
        if isinstance(pred, exp.Between):
            sides += [pred.args.get("low"), pred.args.get("high")]
        elif isinstance(pred, exp.In):
            sides += list(pred.expressions)
        else:
            sides.append(pred.expression)
        return any(s is not None and _direct_column(s, alias) for s in sides)
    return False


def _direct_column(node: exp.Expression, alias: str) -> bool:
    if isinstance(node, exp.Column):
        return node.table == alias
    if isinstance(node, _STRICT_WRAPPERS):
        return any(_direct_column(c, alias) for c in node.iter_expressions() if isinstance(c, exp.Expression))
    return False


def resolve_table(column: exp.Column) -> exp.Table | None:
    """The physical table a column belongs to, resolved through FROM/JOIN aliases of the nearest SELECT."""
    select = column.find_ancestor(exp.Select)
    if select is None:
        return None
    sources: dict[str, exp.Expression] = {}
    from_ = select.args.get("from_") or select.args.get("from")
    items = ([from_.this] if from_ is not None else []) + [j.this for j in select.args.get("joins") or []]
    for item in items:
        if isinstance(item, exp.Expression):
            sources[item.alias_or_name] = item
    if column.table:
        source = sources.get(column.table)
    elif len(sources) == 1:
        source = next(iter(sources.values()))
    else:
        source = None
    return source if isinstance(source, exp.Table) else None


def transform_predicates(root: exp.Expression, fn: Callable[[exp.Expression], exp.Expression | None]) -> list[tuple]:
    """Apply ``fn`` to every predicate-like node (WHERE/ON/HAVING conjunct level and below).

    ``fn`` returns a replacement or ``None``. Returns ``[(old, new), ...]`` in document order.
    Nodes inside the replacement are not revisited.
    """
    changes: list[tuple] = []
    # materialise first: the tree is mutated while iterating
    for node in list(root.walk()):
        if not _attached(node, root):
            continue  # detached by an earlier replacement
        new = fn(node)
        if new is not None and new is not node:
            old = node.copy()
            node.replace(new)
            changes.append((old, new))
    return changes


def _attached(node: exp.Expression, root: exp.Expression) -> bool:
    n = node
    while n is not None:
        if n is root:
            return True
        n = n.parent
    return False


def clause_diff(old: exp.Expression | None, new: exp.Expression | None, dialect: str | None = None) -> tuple[str, str]:
    """(before, after) SQL for a WHERE/HAVING/ON change, reduced to the conjuncts that differ."""
    from ..engine import canonical_sql

    old_parts = [(canonical_sql(c, dialect), c.sql(dialect=dialect)) for c in conjuncts(old)] if old is not None else []
    new_parts = [(canonical_sql(c, dialect), c.sql(dialect=dialect)) for c in conjuncts(new)] if new is not None else []
    old_keys, new_keys = {k for k, _ in old_parts}, {k for k, _ in new_parts}
    removed = [sql for k, sql in old_parts if k not in new_keys]
    added = [sql for k, sql in new_parts if k not in old_keys]
    if not removed and not added:
        return " AND ".join(s for _, s in old_parts), " AND ".join(s for _, s in new_parts)
    return " AND ".join(removed) or "(nothing)", " AND ".join(added) or "(removed)"
