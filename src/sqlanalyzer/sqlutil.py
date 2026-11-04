"""Small SQLGlot helpers shared by the static analyzers."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

_WRAPPERS = (exp.Func, exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod, exp.DPipe)
_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike)


def locate(node: exp.Expr) -> tuple[int | None, int | None]:
    """Best-effort 1-based (line, start column) of ``node`` from tokenizer metadata.

    Uses the earliest token of the subtree (keywords carry no metadata, so a
    ``WHERE`` clause is located at its first operand).
    """
    first: dict | None = None
    for n in node.walk():
        meta = n.meta
        if "line" in meta and (first is None or meta.get("start", 0) < first.get("start", 0)):
            first = meta
    if first is None:
        return None, None
    col = first["col"] - (first.get("end", 0) - first.get("start", 0))
    return first["line"], max(col, 1)


def scopes_of(statement: exp.Expr) -> list[Scope]:
    try:
        return list(traverse_scope(statement))
    except (SqlglotError, KeyError, AttributeError):
        return []


def select_scopes(statement: exp.Expr) -> Iterator[Scope]:
    for scope in scopes_of(statement):
        if isinstance(scope.expression, exp.Select):
            yield scope


def select_of(scope: Scope) -> exp.Select:
    """The SELECT a scope from :func:`select_scopes` wraps (typed; ``Scope.expression`` is untyped)."""
    return cast(exp.Select, scope.expression)


def conjuncts(condition: exp.Expr | None) -> list[exp.Expr]:
    """Flatten ``a AND b AND c`` into ``[a, b, c]`` (parentheses are unwrapped)."""
    if condition is None:
        return []
    if isinstance(condition, exp.Where):
        condition = condition.this
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.And):
        return conjuncts(condition.this) + conjuncts(condition.expression)
    return [condition]


def disjuncts(condition: exp.Expr) -> list[exp.Expr]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, exp.Or):
        return disjuncts(condition.this) + disjuncts(condition.expression)
    return [condition]


def is_wrapped(column: exp.Column, root: exp.Expr) -> bool:
    """True if ``column`` sits under a function/cast/arithmetic inside ``root``.

    Such predicates are non-sargable: they defeat index use and
    partition / min-max pruning.
    """
    node = column.parent
    while node is not None:
        if isinstance(node, _WRAPPERS):
            return True
        if node is root:
            return False
        node = node.parent
    return False


def is_constant(node: exp.Expr) -> bool:
    """No column references and no subqueries below ``node``."""
    return node.find(exp.Column, exp.Subquery, exp.Select) is None


def comparison_sides(node: exp.Expr) -> list[exp.Expr]:
    """Operands of a comparison-like predicate (empty if not a comparison)."""
    if not isinstance(node, _COMPARISONS):
        return []
    sides = [node.this]
    if isinstance(node, exp.Between):
        sides += [node.args.get("low"), node.args.get("high")]
    elif isinstance(node, exp.In):
        sides += list(node.expressions)
        if node.args.get("query") is not None:
            sides.append(node.args["query"])
    else:
        sides.append(node.expression)
    return [s for s in sides if s is not None]


def table_names(table: exp.Table) -> tuple[str, str]:
    """(bare, qualified) lower-cased names of a table reference."""
    bare = table.name.lower()
    parts = [p for p in (table.catalog, table.db, table.name) if p]
    return bare, ".".join(parts).lower()


def real_tables(scope: Scope) -> dict[str, exp.Table]:
    """alias -> Table for physical tables (not CTEs / derived tables) in ``scope``."""
    out: dict[str, exp.Table] = {}
    for alias, (_node, source) in scope.selected_sources.items():
        if isinstance(source, exp.Table):
            out[alias] = source
    return out


def column_owner(column: exp.Column, tables: dict[str, exp.Table]) -> str | None:
    """Alias of the physical table a column belongs to (``None`` if unknown/ambiguous)."""
    if column.table:
        return column.table if column.table in tables else None
    if len(tables) == 1:
        return next(iter(tables))
    return None


def owners_in(node: exp.Expr, tables: dict[str, exp.Table]) -> set[str]:
    return {o for c in node.find_all(exp.Column) if (o := column_owner(c, tables))}


def in_projection(node: exp.Expr) -> bool:
    """True if ``node`` lives in the SELECT list of its nearest enclosing SELECT."""
    child, parent = node, node.parent
    while parent is not None:
        if isinstance(parent, exp.Select):
            return any(child is e for e in parent.expressions)
        child, parent = parent, parent.parent
    return False
