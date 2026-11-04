"""Rules for aggregation, DISTINCT and set operations."""

from __future__ import annotations

from sqlglot import exp

from ...sqlutil import conjuncts
from ..engine import Rewrite, Rule, RuleContext, Safety
from .common import set_where, where_conjuncts


# --------------------------------------------------------------- HAVING -> WHERE
class HavingToWhereRule(Rule):
    name = "having-to-where"
    title = "Move non-aggregate HAVING predicates to WHERE"
    description = ("A HAVING predicate that only references GROUP BY columns can be evaluated before grouping. "
                   "In WHERE it filters rows early (fewer rows to hash/sort) and can use indexes or pruning.")
    category = "aggregation"
    safety = Safety.SAFE
    fixes = ("having-without-aggregate",)

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.Having) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for sel in list(expression.find_all(exp.Select)):
            having = sel.args.get("having")
            group = sel.args.get("group")
            if having is None or group is None:
                continue
            group_keys = {g.sql() for g in group.expressions}
            if any(isinstance(g, exp.Literal) for g in group.expressions):
                continue  # GROUP BY ordinal: cannot map safely
            # output aliases that could shadow an input column in HAVING (``count(*) AS status``)
            aliases = {e.alias.lower() for e in sel.expressions if isinstance(e, exp.Alias) and e.alias
                       and not (isinstance(e.this, exp.Column) and e.this.name.lower() == e.alias.lower())}
            movable, kept = [], []
            for pred in conjuncts(having.this):
                if pred.find(exp.AggFunc, exp.Window, exp.Subquery) is not None:
                    kept.append(pred)
                    continue
                cols = list(pred.find_all(exp.Column))
                if not cols or any(c.sql() not in group_keys or c.name.lower() in aliases for c in cols):
                    kept.append(pred)
                    continue
                movable.append(pred)
            if not movable:
                continue
            before = having.copy()
            set_where(sel, where_conjuncts(sel) + [p.copy() for p in movable])
            if kept:
                sel.set("having", exp.Having(this=exp.and_(*[p.copy() for p in kept], copy=False)))
            else:
                sel.set("having", None)
            after = sel.args.get("having")
            out.append(self.rewrite(before, after if after is not None else "(no HAVING clause)",
                                    "Moved to WHERE: " + ", ".join(p.sql(dialect=ctx.dialect) for p in movable)
                                    + " (only GROUP BY columns, no aggregates).", ctx, node=before))
        return expression, out


# ----------------------------------------------------------- redundant DISTINCT
class RedundantDistinctRule(Rule):
    name = "redundant-distinct"
    title = "Drop DISTINCT when GROUP BY already guarantees unique rows"
    description = ("If every GROUP BY expression is part of the select list, each output row is unique by "
                   "construction; DISTINCT adds a second de-duplication pass for nothing.")
    category = "aggregation"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.Distinct) is not None and expression.find(exp.Group) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for sel in list(expression.find_all(exp.Select)):
            distinct = sel.args.get("distinct")
            group = sel.args.get("group")
            if distinct is None or group is None or distinct.args.get("on"):
                continue
            if not group.expressions or any(isinstance(g, exp.Literal) for g in group.expressions):
                continue
            selected = {e.unalias().sql() for e in sel.expressions}
            selected |= {e.alias for e in sel.expressions if isinstance(e, exp.Alias)}
            if not all(g.sql() in selected for g in group.expressions):
                continue
            sel.set("distinct", None)
            out.append(self.rewrite("SELECT DISTINCT ... GROUP BY " + ", ".join(g.sql(dialect=ctx.dialect)
                                                                                  for g in group.expressions),
                                    "SELECT ... GROUP BY (DISTINCT removed)",
                                    "All GROUP BY keys are in the select list, so rows are already unique.", ctx,
                                    node=sel))
        return expression, out


# ------------------------------------------------------------ UNION -> UNION ALL
class UnionAllRule(Rule):
    name = "union-all"
    title = "Use UNION ALL instead of UNION"
    description = ("UNION de-duplicates the combined result, which requires hashing or sorting every row. "
                   "UNION ALL streams the inputs. Apply it when the branches cannot overlap or duplicates are "
                   "acceptable.")
    category = "set-operations"
    safety = Safety.REVIEW
    fixes = ("union-distinct",)

    def precheck(self, expression: exp.Expr) -> bool:
        return any(u.args.get("distinct") for u in expression.find_all(exp.Union))

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for union in list(expression.find_all(exp.Union)):
            if not union.args.get("distinct"):
                continue
            union.set("distinct", False)
            out.append(self.rewrite("UNION", "UNION ALL", "UNION replaced by UNION ALL; duplicates across the "
                                    "branches are no longer removed.", ctx, node=union))
        return expression, out
