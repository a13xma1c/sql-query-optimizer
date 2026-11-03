"""Rules that wrap SQLGlot's scope-aware optimizer passes.

These passes need every column resolved to its source table (``requires_qualified``),
which the engine guarantees before running them.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable

from sqlglot import exp
from sqlglot.optimizer.eliminate_joins import eliminate_joins
from sqlglot.optimizer.eliminate_subqueries import eliminate_subqueries
from sqlglot.optimizer.merge_subqueries import merge_subqueries
from sqlglot.optimizer.optimize_joins import optimize_joins
from sqlglot.optimizer.pushdown_predicates import pushdown_predicates
from sqlglot.optimizer.pushdown_projections import pushdown_projections
from sqlglot.optimizer.unnest_subqueries import unnest_subqueries

from ..engine import Rewrite, Rule, RuleContext, Safety, canonical_sql
from .common import clause_diff


class _PassRule(Rule):
    """Base: run a SQLGlot pass and describe the change per SELECT block."""

    requires_qualified = True
    pass_fn: Callable[..., exp.Expression]
    pass_kwargs: dict = {}

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        before = expression.copy()
        kwargs = dict(self.pass_kwargs)
        params = inspect.signature(self.pass_fn).parameters
        if "dialect" in params:
            kwargs["dialect"] = ctx.dialect
        after = self.pass_fn(expression, **kwargs)
        return after, self.describe(before, after, ctx)

    def describe(self, before: exp.Expression, after: exp.Expression, ctx: RuleContext) -> list[Rewrite]:
        out: list[Rewrite] = []
        old_ctes = {c.alias for c in before.find_all(exp.CTE)}
        new_ctes = {c.alias for c in after.find_all(exp.CTE)}
        gone = sorted(old_ctes - new_ctes)
        if gone:
            out.append(self.rewrite("WITH " + ", ".join(gone), "(removed)",
                                    f"Unreferenced CTE(s) removed: {', '.join(gone)}.", ctx, node=before))
        old_sel, new_sel = list(before.find_all(exp.Select)), list(after.find_all(exp.Select))
        if len(old_sel) == len(new_sel):
            for o, n in zip(old_sel, new_sel, strict=True):
                changed = self.changed_clauses(o, n, ctx)
                for label, ob, nb in changed:
                    out.append(self.rewrite(ob, nb, label, ctx, node=o))
        if not out and canonical_sql(before, ctx.dialect) != canonical_sql(after, ctx.dialect):
            out.append(self.rewrite(before, after, self.whole_query_text, ctx, node=before))
        return out

    whole_query_text = "Query restructured."

    def changed_clauses(self, o: exp.Select, n: exp.Select, ctx: RuleContext) -> list[tuple[str, object, object]]:
        out: list[tuple[str, object, object]] = []
        d = ctx.dialect
        for key, label in (("where", "WHERE"), ("having", "HAVING")):
            ob, nb = o.args.get(key), n.args.get(key)
            if _canon(ob, d) != _canon(nb, d):
                removed, added = clause_diff(ob.this if ob else None, nb.this if nb else None, d)
                if added in ("TRUE", "(removed)"):
                    out.append((f"Predicate(s) moved out of this {label} clause.", removed, "(moved)"))
                elif removed in ("TRUE", "(nothing)"):
                    out.append((f"Predicate(s) pushed into this {label} clause.", "(none)", added))
                else:
                    out.append((f"{label} clause changed.", removed, added))
        oj, nj = o.args.get("joins") or [], n.args.get("joins") or []
        if len(oj) == len(nj):
            for a, b in zip(oj, nj, strict=True):
                if _canon(a, d) != _canon(b, d):
                    out.append(("Join changed.", a, b))
        elif oj or nj:
            out.append((f"Joins changed ({len(oj)} -> {len(nj)}).", " ".join(j.sql(dialect=d) for j in oj) or "(none)",
                        " ".join(j.sql(dialect=d) for j in nj) or "(none)"))
        if len(o.expressions) != len(n.expressions):
            out.append((f"Select list changed ({len(o.expressions)} -> {len(n.expressions)} columns).",
                        ", ".join(e.sql(dialect=d) for e in o.expressions),
                        ", ".join(e.sql(dialect=d) for e in n.expressions)))
        return out


class _SubqueryPassRule(_PassRule):
    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Subquery, exp.CTE) is not None


class PushdownPredicatesRule(_PassRule):
    name = "pushdown-predicates"
    title = "Push predicates down into subqueries, CTEs and joins"
    description = ("Filters that reference a single subquery, CTE or joined table are moved as close to the "
                   "data as possible (into the derived table, or into the JOIN ... ON clause). Rows are "
                   "discarded before they are joined or materialised.")
    category = "predicates"
    safety = Safety.SAFE
    pass_fn = staticmethod(pushdown_predicates)
    whole_query_text = "Predicates pushed closer to the scanned tables."


class PushdownProjectionsRule(_SubqueryPassRule):
    name = "pushdown-projections"
    title = "Prune unused columns from subqueries and CTEs"
    description = ("Columns selected in a derived table or CTE but never used by the outer query are removed, so "
                   "less data is read, shuffled and materialised (a big win on columnar engines).")
    category = "projection"
    safety = Safety.SAFE
    pass_fn = staticmethod(pushdown_projections)
    whole_query_text = "Unused columns removed from subqueries."


class MergeSubqueriesRule(_SubqueryPassRule):
    name = "merge-subqueries"
    title = "Merge simple derived tables and CTEs into the outer query"
    description = ("A derived table or CTE that is only a projection/filter over its source is inlined into "
                   "the outer query. That removes an optimization fence (relevant for engines that "
                   "materialise CTEs) and gives the planner a flat join graph.")
    category = "subqueries"
    safety = Safety.SAFE
    pass_fn = staticmethod(merge_subqueries)
    whole_query_text = "Subquery merged into its outer query."

    def describe(self, before: exp.Expression, after: exp.Expression, ctx: RuleContext) -> list[Rewrite]:
        nb, na = _count_sources(before), _count_sources(after)
        if nb == na:
            return []
        return [self.rewrite(before, after, f"{nb - na} derived table(s)/CTE(s) merged into the outer query.",
                             ctx, node=before)]


class EliminateSubqueriesRule(_SubqueryPassRule):
    name = "dedupe-subqueries"
    title = "Turn repeated identical subqueries into one CTE"
    description = ("The same derived table appears more than once; it is defined once as a CTE and referenced "
                   "twice, so the engine can recognise (and some can reuse) the shared work, and the query "
                   "becomes shorter.")
    category = "subqueries"
    safety = Safety.SAFE
    fixes = ("repeated-scan",)
    pass_fn = staticmethod(eliminate_subqueries)

    def describe(self, before: exp.Expression, after: exp.Expression, ctx: RuleContext) -> list[Rewrite]:
        nb, na = _count_sources(before), _count_sources(after)
        if nb <= na:
            return []
        return [self.rewrite(before, after, f"{nb - na} duplicated derived table(s) replaced by CTE references.",
                             ctx, node=before)]


class UnnestSubqueriesRule(_SubqueryPassRule):
    name = "unnest-subqueries"
    title = "Convert IN / correlated subqueries into joins"
    description = ("Scalar and IN subqueries that depend on the outer row are executed per row on some engines. "
                   "They are rewritten as joins against a grouped derived table, which is evaluated once. "
                   "The generated SQL is more verbose; review it before adopting.")
    category = "subqueries"
    safety = Safety.REVIEW
    fixes = ("correlated-subquery",)
    pass_fn = staticmethod(unnest_subqueries)
    whole_query_text = "Subquery converted into a join."


class EliminateJoinsRule(_PassRule):
    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Join) is not None

    name = "eliminate-joins"
    title = "Remove joins whose columns are never used"
    description = ("A LEFT JOIN to a derived table that is unique on the join key and whose columns are never "
                   "referenced does not change the result; it is removed.")
    category = "joins"
    safety = Safety.SAFE
    pass_fn = staticmethod(eliminate_joins)
    whole_query_text = "Unused join removed."


class OptimizeJoinsRule(_PassRule):
    def precheck(self, expression: exp.Expression) -> bool:
        return expression.find(exp.Join) is not None

    name = "optimize-joins"
    title = "Move join predicates into ON and order joins by dependency"
    description = ("Cross joins that are actually constrained by predicates elsewhere get those predicates as "
                   "their ON condition, and joins are re-ordered so each one can reference what precedes it.")
    category = "joins"
    safety = Safety.SAFE
    pass_fn = staticmethod(optimize_joins)
    whole_query_text = "Joins reorganised."


def _canon(node: exp.Expression | None, dialect: str) -> str:
    return canonical_sql(node, dialect) if node is not None else ""


def _count_sources(expression: exp.Expression) -> int:
    return sum(1 for n in expression.find_all(exp.Subquery, exp.CTE) if isinstance(n.parent, (exp.From, exp.Join, exp.With)))


SQLGLOT_PASS_RULES: list[type[_PassRule]] = [
    PushdownProjectionsRule,
    PushdownPredicatesRule,
    OptimizeJoinsRule,
    MergeSubqueriesRule,
    EliminateSubqueriesRule,
    EliminateJoinsRule,
    UnnestSubqueriesRule,
]
