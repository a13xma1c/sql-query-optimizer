"""Rewrite rules that only make sense on particular engines (``Rule.engines``)."""

from __future__ import annotations

from sqlglot import exp

from ..engine import Rewrite, Rule, RuleContext, Safety
from .common import is_literal_constant, transform_predicates

LONG_IN_LIST = 100


class ApproxCountDistinctRule(Rule):
    name = "approx-count-distinct"
    title = "Use an approximate distinct count on distributed engines"
    description = ("``COUNT(DISTINCT x)`` makes Trino/Snowflake shuffle and de-duplicate every value exactly. "
                   "``APPROX_DISTINCT`` / ``APPROX_COUNT_DISTINCT`` (HyperLogLog) runs in constant memory with "
                   "~2% error - usually fine for dashboards, never for billing.")
    category = "aggregation"
    safety = Safety.REVIEW
    engines = frozenset({"trino", "snowflake"})
    fixes = ("count-distinct-exact",)

    def precheck(self, expression: exp.Expr) -> bool:
        return any(isinstance(c.this, exp.Distinct) for c in expression.find_all(exp.Count))

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for count in list(expression.find_all(exp.Count)):
            distinct = count.this
            if not isinstance(distinct, exp.Distinct) or len(distinct.expressions) != 1:
                continue
            new = exp.ApproxDistinct(this=distinct.expressions[0].copy())
            out.append(self.rewrite(count, new, "Exact distinct count replaced by a HyperLogLog estimate "
                                    "(~2% relative error).", ctx))
            count.replace(new)
        return expression, out


class CaseInsensitiveLikeRule(Rule):
    name = "ilike"
    title = "Replace LOWER(col) LIKE by ILIKE"
    description = ("``LOWER(col) LIKE 'abc%'`` wraps the column in a function, so no index or zone map applies. "
                   "``col ILIKE 'abc%'`` expresses the same case-insensitive match on the bare column "
                   "(PostgreSQL can serve it from a trigram index). Case folding of non-ASCII characters may differ.")
    category = "predicates"
    safety = Safety.REVIEW
    engines = frozenset({"postgres", "duckdb", "snowflake"})
    fixes = ("case-insensitive-like",)

    def precheck(self, expression: exp.Expr) -> bool:
        return any(isinstance(like.this, (exp.Lower, exp.Upper)) for like in expression.find_all(exp.Like))

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expr) -> exp.Expr | None:
            if not isinstance(node, exp.Like) or node.args.get("escape") is not None:
                return None
            wrapper, pattern = node.this, node.expression
            if not isinstance(wrapper, (exp.Lower, exp.Upper)) or not isinstance(wrapper.this, exp.Column):
                return None
            if not (isinstance(pattern, exp.Literal) and pattern.is_string):
                return None
            text = pattern.this
            folded = text.lower() if isinstance(wrapper, exp.Lower) else text.upper()
            if text != folded:
                return None  # LOWER(col) LIKE 'Abc' can never match; leave it for the user to see
            new = exp.ILike(this=wrapper.this.copy(), expression=pattern.copy())
            out.append(self.rewrite(node, new, "Case-insensitive LIKE expressed on the bare column.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


class InListToAnyRule(Rule):
    name = "in-list-to-any"
    title = "Turn very long IN lists into = ANY(ARRAY[...])"
    description = ("PostgreSQL plans ``col IN (v1, ..., vN)`` as N comparisons; ``col = ANY(ARRAY[...])`` is a "
                   "single array operation that is cheaper to plan, bindable as one parameter and index friendly.")
    category = "predicates"
    safety = Safety.SAFE
    engines = frozenset({"postgres"})
    fixes = ("long-in-list",)

    def precheck(self, expression: exp.Expr) -> bool:
        return any(len(i.expressions) >= LONG_IN_LIST for i in expression.find_all(exp.In))

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expr) -> exp.Expr | None:
            if not isinstance(node, exp.In) or node.args.get("query") is not None or node.args.get("unnest"):
                return None
            values = node.expressions
            if len(values) < LONG_IN_LIST or not all(is_literal_constant(v) for v in values):
                return None
            if isinstance(node.parent, exp.Not):
                return None
            array = exp.Array(expressions=[v.copy() for v in values])
            new = exp.EQ(this=node.this.copy(), expression=exp.Any(this=exp.Paren(this=array)))
            out.append(self.rewrite(f"{node.this.sql(dialect=ctx.dialect)} IN (... {len(values)} values ...)",
                                    f"{node.this.sql(dialect=ctx.dialect)} = ANY(ARRAY[... {len(values)} values ...])",
                                    f"IN list with {len(values)} literals turned into a single array comparison.",
                                    ctx, node=node))
            return new

        transform_predicates(expression, fn)
        return expression, out


ENGINE_SPECIFIC_RULES: list[type[Rule]] = [ApproxCountDistinctRule, CaseInsensitiveLikeRule, InListToAnyRule]
