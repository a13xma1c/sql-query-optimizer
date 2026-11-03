"""Rules that rewrite predicates: sargability, NULL handling, boolean simplification."""

from __future__ import annotations

import datetime as dt

from sqlglot import exp
from sqlglot.optimizer.simplify import Simplifier

from ...sqlutil import disjuncts
from ..engine import Rewrite, Rule, RuleContext, Safety
from .common import (
    clause_diff,
    date_literal,
    is_literal_constant,
    literal_int,
    literal_string,
    parse_date,
    resolve_table,
    transform_predicates,
)

_ORDER = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE)
_DATE_TYPES = {exp.DataType.Type.DATE, exp.DataType.Type.DATETIME, exp.DataType.Type.TIMESTAMP,
               exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMESTAMPLTZ, exp.DataType.Type.TIMESTAMPNTZ,
               exp.DataType.Type.DATE32, exp.DataType.Type.DATETIME64, exp.DataType.Type.TIMESTAMP_S,
               exp.DataType.Type.TIMESTAMP_MS, exp.DataType.Type.TIMESTAMP_NS}


# ------------------------------------------------------------------ NULL comparisons
class NullComparisonRule(Rule):
    name = "null-comparison"
    title = "Use IS NULL instead of = NULL"
    description = ("``x = NULL`` and ``x <> NULL`` are never true (NULL compares as unknown). "
                   "They are rewritten to ``IS NULL`` / ``IS NOT NULL``, which is almost always the intent.")
    category = "predicates"
    safety = Safety.REVIEW  # the original predicate filters out every row, the rewrite does not
    fixes = ("null-comparison",)

    def precheck(self, expression: exp.Expression) -> bool:
        return any(isinstance(n.parent, (exp.EQ, exp.NEQ)) for n in expression.find_all(exp.Null))

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        def fn(node: exp.Expression) -> exp.Expression | None:
            if not isinstance(node, (exp.EQ, exp.NEQ)):
                return None
            left, right = node.this, node.expression
            if isinstance(right, exp.Null) and not isinstance(left, exp.Null):
                other = left
            elif isinstance(left, exp.Null) and not isinstance(right, exp.Null):
                other = right
            else:
                return None
            is_ = exp.Is(this=other.copy(), expression=exp.Null())
            return exp.Not(this=is_) if isinstance(node, exp.NEQ) else is_

        out = []
        for old, new in transform_predicates(expression, fn):
            out.append(self.rewrite(old, new, "Comparison with NULL through =/<> can never be true; "
                                    "IS [NOT] NULL tests for NULL values as intended.", ctx))
        return expression, out


# --------------------------------------------------------------- sargable date ranges
class SargableDateRule(Rule):
    name = "sargable-date"
    title = "Rewrite DATE()/YEAR() predicates as ranges"
    description = ("A predicate such as ``DATE(ts) = '2024-01-15'`` or ``YEAR(ts) = 2024`` wraps the column in a "
                   "function, so indexes, zone maps and partition pruning cannot be used. It is rewritten as an "
                   "equivalent range on the bare column (``ts >= DATE '2024-01-15' AND ts < DATE '2024-01-16'``).")
    category = "predicates"
    safety = Safety.SAFE
    fixes = ("non-sargable-predicate", "partition-column-wrapped")

    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Date, exp.TsOrDsToDate, exp.Year, exp.Extract, exp.Cast) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expression) -> exp.Expression | None:
            if isinstance(node, exp.Between):
                return self._between(node, ctx, out)
            if not isinstance(node, _ORDER):
                return None
            left, right = node.this, node.expression
            wrapped = _unwrap_date(left)
            op: type[exp.Expression] = type(node)
            if wrapped is None:
                wrapped = _unwrap_date(right)
                if wrapped is None:
                    return None
                left, right = right, left
                op = _mirror(op)
            column, unit = wrapped
            if not isinstance(column, exp.Column):
                return None
            bounds = _bounds(unit, right)
            if bounds is None:
                return None
            lo, hi = bounds  # the wrapped value equals the literal iff lo <= column < hi
            if _column_is_temporal(column, ctx) is not True and not ctx.include_review:
                return None
            col = column.copy()
            if op is exp.EQ:
                new: exp.Expression = exp.and_(exp.GTE(this=col, expression=date_literal(lo)),
                                               exp.LT(this=col.copy(), expression=date_literal(hi)), copy=False)
            elif op is exp.GTE:
                new = exp.GTE(this=col, expression=date_literal(lo))
            elif op is exp.GT:
                new = exp.GTE(this=col, expression=date_literal(hi))
            elif op is exp.LT:
                new = exp.LT(this=col, expression=date_literal(lo))
            else:  # LTE
                new = exp.LT(this=col, expression=date_literal(hi))
            if isinstance(node.parent, (exp.And, exp.Or, exp.Not)) and isinstance(new, exp.And):
                new = exp.paren(new, copy=False)
            out.append(self._describe(node, new, column, ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out

    def _between(self, node: exp.Between, ctx: RuleContext, out: list[Rewrite]) -> exp.Expression | None:
        wrapped = _unwrap_date(node.this)
        if wrapped is None or not isinstance(wrapped[0], exp.Column):
            return None
        column, unit = wrapped
        lo = _bounds(unit, node.args["low"])
        hi = _bounds(unit, node.args["high"])
        if lo is None or hi is None:
            return None
        if _column_is_temporal(column, ctx) is not True and not ctx.include_review:
            return None
        new = exp.and_(exp.GTE(this=column.copy(), expression=date_literal(lo[0])),
                       exp.LT(this=column.copy(), expression=date_literal(hi[1])), copy=False)
        if isinstance(node.parent, (exp.And, exp.Or, exp.Not)):
            new = exp.paren(new, copy=False)
        out.append(self._describe(node, new, column, ctx))
        return new

    def _describe(self, old: exp.Expression, new: exp.Expression, column: exp.Column, ctx: RuleContext) -> Rewrite:
        text = (f"'{column.sql(dialect=ctx.dialect)}' was wrapped in a function inside the predicate, which "
                "prevents index use and partition pruning; the equivalent range lets the engine seek/prune.")
        if _column_is_temporal(column, ctx) is True:
            return self.rewrite(old, new, text, ctx)
        text += " Assumes the column is of a DATE/TIMESTAMP type (declare the schema to confirm)."
        return self.rewrite(old, new, text, ctx, safety=Safety.REVIEW)


def _unwrap_date(node: exp.Expression) -> tuple[exp.Expression, str] | None:
    """(inner expression, unit) for DATE(x), CAST(x AS DATE), x::date, YEAR(x), EXTRACT(YEAR FROM x)."""
    if isinstance(node, exp.Date) and node.expressions == [] and not node.args.get("zone"):
        return node.this, "day"
    if isinstance(node, exp.TsOrDsToDate) and not node.args.get("format"):
        return node.this, "day"  # Snowflake/Trino DATE(x)
    if isinstance(node, exp.Cast) and node.to.is_type(exp.DataType.Type.DATE) and not node.args.get("format"):
        return node.this, "day"
    if isinstance(node, exp.Year):
        return node.this, "year"
    if isinstance(node, exp.Extract) and isinstance(node.this, (exp.Var, exp.Identifier)) \
            and node.this.name.upper() == "YEAR":
        return node.expression, "year"
    return None


def _bounds(unit: str, literal: exp.Expression) -> tuple[dt.date, dt.date] | None:
    if unit == "day":
        text = literal_string(literal)
        if text is None:
            return None
        day = parse_date(text)
        if day is None:
            return None
        return day, day + dt.timedelta(days=1)
    year = literal_int(literal)
    if year is None or not 1 <= year <= 9998:
        return None
    return dt.date(year, 1, 1), dt.date(year + 1, 1, 1)


def _mirror(op: type[exp.Expression]) -> type[exp.Expression]:
    return {exp.GT: exp.LT, exp.LT: exp.GT, exp.GTE: exp.LTE, exp.LTE: exp.GTE}.get(op, op)


def _column_is_temporal(column: exp.Column, ctx: RuleContext) -> bool | None:
    """True/False if the schema knows the column type, None if unknown."""
    if not ctx.schema:
        return None
    from sqlglot.schema import ensure_schema

    table = resolve_table(column)
    if table is None:
        return None
    try:
        schema = ensure_schema(ctx.schema, dialect=ctx.dialect)
        typ = schema.get_column_type(table, column.name, dialect=ctx.dialect)
    except Exception:  # noqa: BLE001 - unknown table/column
        return None
    if typ is None or typ.this in (exp.DataType.Type.UNKNOWN, exp.DataType.Type.NULL):
        return None
    return typ.this in _DATE_TYPES


# -------------------------------------------------------------------------- OR -> IN
class OrToInRule(Rule):
    name = "or-to-in"
    title = "Merge OR-ed equalities into IN"
    description = ("``c = 1 OR c = 2 OR c = 3`` becomes ``c IN (1, 2, 3)``: shorter, and engines evaluate an "
                   "IN list with a hash set or a single index range instead of separate predicates.")
    category = "predicates"
    safety = Safety.SAFE
    fixes = ("or-across-columns",)

    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Or) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expression) -> exp.Expression | None:
            if not isinstance(node, exp.Or):
                return None
            if isinstance(node.parent, exp.Or):
                return None  # handled at the top of the OR chain
            branches = disjuncts(node)
            groups: dict[str, list[exp.Expression]] = {}
            order: list[str] = []
            others: list[exp.Expression] = []
            keys: dict[str, exp.Expression] = {}
            for b in branches:
                key, values = _equality_values(b)
                if key is None:
                    others.append(b)
                    continue
                if key not in groups:
                    groups[key] = []
                    order.append(key)
                    keys[key] = values[0]
                groups[key] += values[1:]
            merged = [k for k in order if len(groups[k]) >= 2]
            if not merged:
                return None
            new_parts: list[exp.Expression] = []
            for k in order:
                if len(groups[k]) >= 2:
                    seen: set[str] = set()
                    vals = []
                    for v in groups[k]:
                        s = v.sql()
                        if s not in seen:
                            seen.add(s)
                            vals.append(v.copy())
                    new_parts.append(exp.In(this=keys[k].copy(), expressions=vals))
                else:
                    new_parts += [b.copy() for b in branches if _equality_values(b)[0] == k]
            new_parts += [b.copy() for b in others]
            new = exp.or_(*new_parts, copy=False) if len(new_parts) > 1 else new_parts[0]
            if isinstance(node.parent, (exp.And, exp.Not)) and isinstance(new, exp.Or):
                new = exp.paren(new, copy=False)
            out.append(self.rewrite(node, new, f"{len(merged)} column(s) compared with several constants through "
                                    "OR; an IN list is evaluated more efficiently.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


def _equality_values(pred: exp.Expression) -> tuple[str | None, list[exp.Expression]]:
    """('col sql', [col, v1, v2]) for ``col = v`` / ``col IN (v1, v2)`` with constant values."""
    while isinstance(pred, exp.Paren):
        pred = pred.this
    if isinstance(pred, exp.EQ):
        left, right = pred.this, pred.expression
        if isinstance(right, exp.Column) and is_literal_constant(left):
            left, right = right, left
        if isinstance(left, exp.Column) and is_literal_constant(right) and not isinstance(right, exp.Null):
            return left.sql(), [left, right]
    if isinstance(pred, exp.In) and pred.args.get("query") is None and not pred.args.get("unnest"):
        if isinstance(pred.this, exp.Column) and pred.expressions and all(
                is_literal_constant(v) and not isinstance(v, exp.Null) for v in pred.expressions):
            return pred.this.sql(), [pred.this, *pred.expressions]
    return None, []


# ---------------------------------------------------------------- single-value IN
class SingleValueInRule(Rule):
    name = "single-value-in"
    title = "Replace IN with a single value by ="
    description = "``x IN (v)`` is the same as ``x = v`` (and ``NOT IN (v)`` the same as ``<> v``); the plain comparison is simpler for the planner."
    category = "predicates"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expression) -> bool:
        return any(len(n.expressions) == 1 for n in expression.find_all(exp.In))

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []

        def single(node: exp.Expression) -> exp.Expression | None:
            if (isinstance(node, exp.In) and node.args.get("query") is None and not node.args.get("unnest")
                    and len(node.expressions) == 1 and not isinstance(node.expressions[0], (exp.Query, exp.Subquery))):
                return node.expressions[0]
            return None

        def fn(node: exp.Expression) -> exp.Expression | None:
            if isinstance(node, exp.Not):
                value = single(node.this)
                if value is None:
                    return None
                new: exp.Expression = exp.NEQ(this=node.this.this.copy(), expression=value.copy())
            else:
                value = single(node)
                if value is None:
                    return None
                new = exp.EQ(this=node.this.copy(), expression=value.copy())
            out.append(self.rewrite(node, new, "IN list with a single element rewritten as a comparison.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


# ------------------------------------------------------------- LIKE without wildcard
class LikeWithoutWildcardRule(Rule):
    name = "like-to-equality"
    title = "Replace LIKE without wildcards by ="
    description = ("``x LIKE 'abc'`` has no ``%``/``_`` wildcard, so it is a plain equality. ``=`` can use "
                   "hash joins, indexes and statistics that LIKE cannot.")
    category = "predicates"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Like) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expression) -> exp.Expression | None:
            if not isinstance(node, exp.Like) or node.args.get("escape") is not None:
                return None
            pattern = node.expression
            if not (isinstance(pattern, exp.Literal) and pattern.is_string):
                return None
            if any(ch in pattern.this for ch in "%_\\"):
                return None
            new = exp.EQ(this=node.this.copy(), expression=pattern.copy())
            out.append(self.rewrite(node, new, "LIKE pattern contains no wildcard; equality is cheaper.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


# ------------------------------------------------------------------- prefix matching
class PrefixMatchRule(Rule):
    name = "prefix-match"
    title = "Rewrite SUBSTR(x, 1, n) = 'abc' as x LIKE 'abc%'"
    description = ("Comparing a leading substring with a constant wraps the column in a function, which blocks "
                   "index use. ``x LIKE 'abc%'`` is equivalent and can use a prefix (B-tree) index or min/max pruning.")
    category = "predicates"
    safety = Safety.SAFE
    fixes = ("non-sargable-predicate",)

    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Substring, exp.Left) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expression) -> exp.Expression | None:
            if not isinstance(node, exp.EQ):
                return None
            left, right = node.this, node.expression
            if isinstance(right, (exp.Substring, exp.Left)):
                left, right = right, left
            prefix = _prefix_of(left)
            if prefix is None:
                return None
            column, length = prefix
            text = literal_string(right)
            if text is None or len(text) != length or any(ch in text for ch in "%_\\"):
                return None
            new = exp.Like(this=column.copy(), expression=exp.Literal.string(text + "%"))
            out.append(self.rewrite(node, new, "Leading-substring comparison rewritten as an anchored LIKE, "
                                    "which is sargable.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


def _prefix_of(node: exp.Expression) -> tuple[exp.Expression, int] | None:
    if isinstance(node, exp.Substring):
        start, length = literal_int(node.args.get("start")), literal_int(node.args.get("length"))
        if start == 1 and length is not None and length > 0 and isinstance(node.this, exp.Column):
            return node.this, length
    if isinstance(node, exp.Left):
        length = literal_int(node.expression)
        if length is not None and length > 0 and isinstance(node.this, exp.Column):
            return node.this, length
    return None


# -------------------------------------------------------------------- simplification
class _StableSimplifier(Simplifier):
    """SQLGlot's simplifier without the cosmetic re-ordering of operands."""

    def uniq_sort(self, expression, root=True):  # type: ignore[override]
        if isinstance(expression, exp.Connector) and not isinstance(expression, exp.Xor) and (
                root or not expression.same_parent):
            flattened = list(expression.flatten())
            seen: dict[str, exp.Expression] = {}
            for e in flattened:
                seen.setdefault(e.sql(), e)
            if len(seen) < len(flattened):
                if len(seen) == 1:
                    return next(iter(seen.values()))
                func = exp.and_ if isinstance(expression, exp.And) else exp.or_
                return func(*seen.values(), copy=False)
        return expression

    def sort_comparison(self, expression):  # type: ignore[override]
        # only move constants to the right-hand side: 5 = x -> x = 5
        if expression.__class__ in self.COMPLEMENT_COMPARISONS:
            left, right = expression.this, expression.expression
            if is_literal_constant(left) and not is_literal_constant(right) and isinstance(right, exp.Column):
                return self.INVERSE_COMPARISONS.get(expression.__class__, expression.__class__)(
                    this=right, expression=left)
        return expression

    def rewrite_between(self, expression):  # type: ignore[override]
        return expression


class SimplifyRule(Rule):
    name = "simplify"
    title = "Simplify constant and boolean expressions"
    description = ("Folds constants (``1 + 1``), removes tautologies (``1 = 1``, ``TRUE AND x``), double negations "
                   "and absorbed terms (``a OR (a AND b)``), and turns ``DATE_TRUNC('day', ts) = d`` into a range. "
                   "Less work for the planner and clearer predicates.")
    category = "predicates"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Condition) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        before = expression.copy()
        simplified = _StableSimplifier(ctx.dialect).simplify(expression)
        if before == simplified:
            return simplified, []
        out: list[Rewrite] = []
        for old_sel, new_sel in zip(before.find_all(exp.Select), simplified.find_all(exp.Select), strict=False):
            for key in ("where", "having", "qualify"):
                o, n = old_sel.args.get(key), new_sel.args.get(key)
                if o != n:
                    before_sql, after_sql = clause_diff(o.this if o else None, n.this if n else None, ctx.dialect)
                    out.append(self.rewrite(before_sql, after_sql, f"{key.upper()} clause simplified.", ctx, node=o))
            for oj, nj in zip(old_sel.args.get("joins") or [], new_sel.args.get("joins") or [], strict=False):
                if oj != nj:
                    out.append(self.rewrite(oj, nj, "Join condition simplified.", ctx))
            for oe, ne in zip(old_sel.expressions, new_sel.expressions, strict=False):
                if oe != ne:
                    out.append(self.rewrite(oe, ne, "Select expression simplified.", ctx))
        if not out:
            out.append(self.rewrite(before, simplified, "Expression simplified.", ctx))
        return simplified, out
