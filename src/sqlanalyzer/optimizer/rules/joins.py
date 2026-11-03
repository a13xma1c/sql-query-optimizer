"""Rules that restructure joins."""

from __future__ import annotations

from sqlglot import exp

from ...sqlutil import scopes_of, select_scopes
from ..engine import Rewrite, Rule, RuleContext, Safety
from .common import column_tables, is_null_rejecting, set_where, where_conjuncts


def _source_aliases(select: exp.Select) -> list[str]:
    """Aliases/names of the FROM item and every join target, in order."""
    names: list[str] = []
    from_ = select.args.get("from_") or select.args.get("from")
    if from_ is not None and isinstance(from_.this, exp.Expression):
        names.append(from_.this.alias_or_name)
    for j in select.args.get("joins") or []:
        names.append(j.this.alias_or_name)
    return names


# ------------------------------------------------------------------ comma joins
class ExplicitJoinRule(Rule):
    name = "explicit-join"
    title = "Turn comma joins into explicit JOIN ... ON"
    description = ("``FROM a, b WHERE a.id = b.a_id`` is rewritten as ``FROM a JOIN b ON a.id = b.a_id``. The "
                   "result is identical, but the join condition is visible to readers and to the optimizer, "
                   "and an accidental Cartesian product becomes obvious (it is kept as an explicit CROSS JOIN).")
    category = "joins"
    safety = Safety.SAFE
    fixes = ("implicit-join", "cartesian-join")

    def precheck(self, expression: exp.Expression) -> bool:
        return any(_is_comma_join(j) for j in expression.find_all(exp.Join))

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []
        for scope in select_scopes(expression):
            sel: exp.Select = scope.expression
            joins: list[exp.Join] = sel.args.get("joins") or []
            comma = [j for j in joins if _is_comma_join(j)]
            if not comma:
                continue
            preds = where_conjuncts(sel)
            aliases = _source_aliases(sel)
            remaining = list(preds)
            for join in joins:
                if not _is_comma_join(join):
                    continue
                target = join.this
                if not isinstance(target, (exp.Table, exp.Subquery)):
                    continue
                alias = target.alias_or_name
                earlier = set(aliases[: aliases.index(alias)]) if alias in aliases else set()
                picked = []
                for p in remaining:
                    tabs = column_tables(p)
                    if alias in tabs and tabs - {alias} and tabs - {alias} <= earlier and p.find(exp.Subquery) is None:
                        picked.append(p)
                before = join.copy()
                if picked:
                    for p in picked:
                        remaining.remove(p)
                    join.set("on", exp.and_(*[p.copy() for p in picked], copy=False))
                    join.set("kind", None)
                    join.set("side", None)
                    text = (f"Join to '{alias}' was expressed through the WHERE clause; its {len(picked)} "
                            "predicate(s) moved into an explicit JOIN ... ON.")
                else:
                    join.set("kind", "CROSS")
                    text = (f"'{alias}' has no join predicate in WHERE; made the Cartesian product an explicit "
                            "CROSS JOIN so it is visible (add a join condition if this is unintended).")
                out.append(self.rewrite(before, join, text, ctx))
            if len(remaining) != len(preds):
                set_where(sel, remaining)
        return expression, out


def _is_comma_join(join: exp.Join) -> bool:
    return not (join.side or join.kind or join.args.get("on") or join.args.get("using") or join.args.get("method"))


# ------------------------------------------------------------ LEFT JOIN -> INNER
class LeftJoinToInnerRule(Rule):
    name = "left-join-to-inner"
    title = "Turn LEFT JOIN into INNER JOIN when WHERE rejects NULLs"
    description = ("A WHERE predicate such as ``b.x = 3`` is never true for the NULL-padded rows a LEFT JOIN "
                   "produces, so the join is effectively inner. Declaring it INNER lets the planner reorder "
                   "joins freely and use cheaper join algorithms.")
    category = "joins"
    safety = Safety.SAFE
    fixes = ("left-join-filtered",)

    def precheck(self, expression: exp.Expression) -> bool:
        return any(j.side == "LEFT" for j in expression.find_all(exp.Join)) and expression.find(exp.Where) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []
        for scope in select_scopes(expression):
            sel: exp.Select = scope.expression
            preds = where_conjuncts(sel)
            if not preds:
                continue
            for join in sel.args.get("joins") or []:
                if (join.side or "").upper() != "LEFT" or join.args.get("method"):
                    continue
                alias = join.this.alias_or_name
                if not alias:
                    continue
                rejecting = [p for p in preds if is_null_rejecting(p, alias)]
                if not rejecting:
                    continue
                before = join.copy()
                join.set("side", None)
                join.set("kind", "INNER")
                out.append(self.rewrite(before, join, f"WHERE predicate {rejecting[0].sql(dialect=ctx.dialect)!r} "
                                        f"rejects NULLs from '{alias}', so unmatched rows were already dropped.", ctx))
        return expression, out


# -------------------------------------------------------------- redundant self checks
class RedundantJoinConditionRule(Rule):
    name = "duplicate-join-predicate"
    title = "Remove join predicates repeated in WHERE"
    description = ("The same equality appears both in JOIN ... ON and in WHERE (common after copy/paste). "
                   "The duplicate is removed from WHERE; it adds filter work without changing the result.")
    category = "joins"
    safety = Safety.SAFE

    def precheck(self, expression: exp.Expression) -> bool:
        return any(j.args.get("on") is not None for j in expression.find_all(exp.Join)) \
            and expression.find(exp.Where) is not None

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        out: list[Rewrite] = []
        for scope in scopes_of(expression):
            sel = scope.expression
            if not isinstance(sel, exp.Select):
                continue
            on_preds: set[str] = set()
            for join in sel.args.get("joins") or []:
                on = join.args.get("on")
                if on is not None and (join.side or "").upper() not in ("LEFT", "RIGHT", "FULL"):
                    for c in _flatten_and(on):
                        on_preds.add(_canonical(c))
            if not on_preds:
                continue
            preds = where_conjuncts(sel)
            keep = [p for p in preds if _canonical(p) not in on_preds]
            if len(keep) != len(preds):
                dropped = [p for p in preds if _canonical(p) in on_preds]
                before = sel.args["where"].copy()
                set_where(sel, keep)
                after = sel.args.get("where")
                out.append(self.rewrite(before, after if after is not None else "(no WHERE clause)",
                                        "Predicate(s) already enforced by an inner JOIN ... ON removed from WHERE: "
                                        + ", ".join(p.sql(dialect=ctx.dialect) for p in dropped), ctx, node=before))
        return expression, out


def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        return _flatten_and(node.this) + _flatten_and(node.expression)
    return [node]


def _canonical(pred: exp.Expression) -> str:
    while isinstance(pred, exp.Paren):
        pred = pred.this
    if isinstance(pred, exp.EQ):
        a, b = pred.this.sql(), pred.expression.sql()
        return "=".join(sorted((a, b)))
    return pred.sql()
