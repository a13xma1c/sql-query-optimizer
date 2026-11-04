"""Rules for subqueries and common table expressions."""

from __future__ import annotations

from sqlglot import exp

from ...sqlutil import conjuncts
from ..engine import Rewrite, Rule, RuleContext, Safety
from .common import transform_predicates


# ---------------------------------------------------------- NOT IN -> NOT EXISTS
class NotInToNotExistsRule(Rule):
    name = "not-in-to-not-exists"
    title = "Rewrite NOT IN (subquery) as NOT EXISTS"
    description = ("``x NOT IN (SELECT y FROM t)`` yields no rows at all if any ``y`` is NULL and is often planned "
                   "as a slow nested loop. ``NOT EXISTS`` with a correlated predicate is planned as an anti-join. "
                   "Results differ only when NULLs are involved, which is usually the intended behaviour.")
    category = "subqueries"
    safety = Safety.REVIEW
    fixes = ("not-in-subquery",)

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.Not, exp.All) is not None and expression.find(exp.Subquery, exp.All) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []

        def fn(node: exp.Expr) -> exp.Expr | None:
            if isinstance(node, exp.Not) and isinstance(node.this, exp.In):
                in_ = node.this
                query = in_.args.get("query")
                if query is None or not isinstance(query, exp.Subquery) or not isinstance(query.this, exp.Select):
                    return None
                inner: exp.Select = query.this
                outer_value = in_.this
            elif isinstance(node, exp.NEQ) and isinstance(node.expression, exp.All) \
                    and isinstance(node.expression.this, exp.Select):
                inner = node.expression.this  # ``x <> ALL (SELECT ...)``: how Snowflake's parser spells NOT IN
                outer_value = node.this
            else:
                return None
            if not _rewritable(inner, outer_value):
                return None
            proj = inner.expressions[0]
            proj_expr = proj.this if isinstance(proj, exp.Alias) else proj
            new_inner = inner.copy()
            new_inner.set("expressions", [exp.Literal.number(1)])
            link = exp.EQ(this=proj_expr.copy(), expression=outer_value.copy())
            preds = conjuncts(new_inner.args.get("where")) + [link]
            new_inner.set("where", exp.Where(this=exp.and_(*preds, copy=False)))
            new = exp.Not(this=exp.Exists(this=new_inner))
            out.append(self.rewrite(node, new, "NOT IN over a subquery rewritten as an anti-join. Note: if the "
                                    "subquery can return NULL the original returns no rows, the rewrite keeps "
                                    "the non-matching rows.", ctx))
            return new

        transform_predicates(expression, fn)
        return expression, out


def _rewritable(inner: exp.Select, outer_value: exp.Expr) -> bool:
    if len(inner.expressions) != 1 or isinstance(inner.expressions[0].unalias(), exp.Star):
        return False
    for key in ("group", "having", "limit", "offset", "distinct", "order", "qualify", "with_", "with"):
        if inner.args.get(key) is not None:
            return False
    if inner.find(exp.AggFunc, exp.Window) is not None:
        return False
    from_ = inner.args.get("from_") or inner.args.get("from")
    if from_ is None:
        return False
    # the outer value must not be captured by the inner FROM: require qualified columns (or constants)
    sources = {from_.this.alias_or_name} | {j.this.alias_or_name for j in inner.args.get("joins") or []}
    for col in outer_value.find_all(exp.Column):
        if not col.table or col.table in sources:
            return False
    if outer_value.find(exp.Subquery, exp.Select) is not None:
        return False
    proj = inner.expressions[0].unalias()
    if isinstance(proj, exp.Column) and not proj.table and len(sources) > 1:
        return False  # ambiguous once correlated
    return True


# ------------------------------------------------------- ORDER BY in subqueries
class SubqueryOrderByRule(Rule):
    name = "subquery-order-by"
    title = "Drop ORDER BY in subqueries without LIMIT"
    description = ("Sorting inside a derived table, CTE or IN/EXISTS subquery that has no LIMIT/OFFSET does not "
                   "order the outer result (SQL makes no such guarantee), yet the engine may still pay for the "
                   "sort. The ORDER BY is removed; keep it if you rely on engine-specific ordering behaviour.")
    category = "subqueries"
    safety = Safety.REVIEW
    fixes = ("order-by-in-subquery",)

    def precheck(self, expression: exp.Expr) -> bool:
        return any(isinstance(o.parent, exp.Select) and o.parent.parent is not None
                   for o in expression.find_all(exp.Order))

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for sel in list(expression.find_all(exp.Select)):
            order = sel.args.get("order")
            if order is None or sel.args.get("limit") is not None or sel.args.get("offset") is not None \
                    or sel.args.get("fetch") is not None:
                continue
            where = _subquery_kind(sel)
            if where is None:
                continue
            if sel.find(exp.Window) is not None or sel.find(exp.ArrayAgg, exp.GroupConcat) is not None:
                continue
            before = order.copy()
            sel.set("order", None)
            out.append(self.rewrite(before, f"(ORDER BY removed from {where})",
                                    f"ORDER BY inside a {where} without LIMIT does not affect the final result.", ctx,
                                    node=before))
        return expression, out


def _subquery_kind(sel: exp.Select) -> str | None:
    """'CTE', 'derived table' or 'IN/EXISTS subquery' when ORDER BY in ``sel`` is pointless, else None."""
    parent = sel.parent
    if isinstance(parent, exp.CTE):
        return "CTE"
    if isinstance(parent, exp.Exists):
        return "EXISTS subquery"
    if isinstance(parent, exp.Subquery):
        grand = parent.parent
        if isinstance(grand, (exp.From, exp.Join)):
            return "derived table"
        if isinstance(grand, (exp.In, exp.Exists)):
            return "IN/EXISTS subquery"
    return None


# ------------------------------------------------------------------ unused CTEs
class UnusedCteRule(Rule):
    name = "unused-cte"
    title = "Remove unreferenced CTEs"
    description = ("A WITH clause defines a CTE that the query never references. Some engines still plan or even "
                   "materialise it; removing it also makes the query shorter.")
    category = "subqueries"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.With) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        changed = True
        while changed:
            changed = False
            for with_ in list(expression.find_all(exp.With)):
                ctes: list[exp.CTE] = list(with_.expressions)
                names = {c.alias.lower() for c in ctes}
                referenced: set[str] = set()
                for table in expression.find_all(exp.Table):
                    if table.db or table.catalog:
                        continue
                    name = table.name.lower()
                    if name not in names:
                        continue
                    owner = table.find_ancestor(exp.CTE)
                    if owner is not None and owner in ctes and owner.alias.lower() == name:
                        continue  # self reference inside its own definition
                    referenced.add(name)
                unused = [c for c in ctes if c.alias.lower() not in referenced]
                if not unused:
                    continue
                for cte in unused:
                    out.append(self.rewrite(cte, "(removed)", f"CTE '{cte.alias}' is never referenced.", ctx))
                    cte.pop()
                    changed = True
                if not with_.expressions:
                    with_.pop()
        return expression, out


# ------------------------------------------------ DISTINCT inside IN / EXISTS
class DistinctInSemiJoinRule(Rule):
    name = "distinct-in-semijoin"
    title = "Drop DISTINCT inside IN / EXISTS subqueries"
    description = ("IN and EXISTS only test membership, so duplicates in the subquery never change the result. "
                   "Removing DISTINCT avoids a needless hash/sort step.")
    category = "subqueries"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.Distinct) is not None and expression.find(exp.In, exp.Exists) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for node in list(expression.find_all(exp.In, exp.Exists)):
            query = node.args.get("query") if isinstance(node, exp.In) else node.this
            if isinstance(query, exp.Subquery):
                query = query.this
            if not isinstance(query, exp.Select) or query.args.get("distinct") is None:
                continue
            if query.args.get("limit") is not None or query.args.get("offset") is not None:
                continue
            distinct = query.args["distinct"]
            if distinct.args.get("on"):
                continue
            before = query.copy()
            query.set("distinct", None)
            kind = "IN" if isinstance(node, exp.In) else "EXISTS"
            out.append(self.rewrite(before, query, f"DISTINCT is redundant in an {kind} subquery.", ctx))
        return expression, out


# ----------------------------------------------------------- EXISTS (SELECT *)
class ExistsProjectionRule(Rule):
    name = "exists-select-1"
    title = "Use SELECT 1 inside EXISTS"
    description = ("The select list of an EXISTS subquery is never evaluated, so ``SELECT *`` or column lists "
                   "are replaced by ``SELECT 1``. Mostly cosmetic, but it avoids column resolution work and "
                   "makes the intent explicit.")
    category = "subqueries"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expr) -> bool:
        return expression.find(exp.Exists) is not None

    def apply(self, expression: exp.Expr, ctx: RuleContext) -> tuple[exp.Expr, list[Rewrite]]:
        out: list[Rewrite] = []
        for node in list(expression.find_all(exp.Exists)):
            query = node.this
            if isinstance(query, exp.Subquery):
                query = query.this
            if not isinstance(query, exp.Select):
                continue
            exprs = query.expressions
            if len(exprs) == 1 and isinstance(exprs[0], exp.Literal):
                continue
            if not all(isinstance(e.unalias(), (exp.Star, exp.Column)) for e in exprs):
                continue
            if any(query.args.get(k) is not None for k in ("group", "having", "distinct", "qualify", "limit")):
                continue
            if query.find(exp.AggFunc, exp.Window) is not None:
                continue
            before = " , ".join(e.sql(dialect=ctx.dialect) for e in exprs)
            query.set("expressions", [exp.Literal.number(1)])
            out.append(self.rewrite(f"EXISTS (SELECT {before} ...)", "EXISTS (SELECT 1 ...)",
                                    "EXISTS ignores the projected columns.", ctx, node=node))
        return expression, out
