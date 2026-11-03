"""Name resolution: qualify columns and expand ``SELECT *`` using a schema."""

from __future__ import annotations

from sqlglot import exp

from ..engine import Rewrite, Rule, RuleContext, Safety, try_qualify


def _select_stars(expression: exp.Expression) -> list[exp.Star | exp.Column]:
    out: list = []
    for star in expression.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Select):
            out.append(star)
        elif isinstance(parent, exp.Column) and isinstance(parent.parent, exp.Select):
            out.append(parent)
    return out


class QualifyRule(Rule):
    """Resolve columns to their tables; expand ``*`` when the schema is known.

    The engine runs this rule once, before all others. It only reports a rewrite
    when a ``SELECT *`` was expanded; the plain qualification (``orders AS orders``,
    ``o.id AS id``) is a prerequisite for the SQLGlot based rules and is not
    reported on its own.
    """

    name = "qualify"
    title = "Resolve column names and expand SELECT *"
    description = ("Qualifies every column with its source table and replaces ``SELECT *`` with the explicit "
                   "column list from the declared schema. Explicit lists read less data on columnar engines and "
                   "are stable when tables gain columns. Requires a schema (or fully qualified, star-free SQL).")
    category = "naming"
    safety = Safety.SAFE
    fixes = ("select-star",)

    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        if ctx.qualified:
            return expression, []
        qualified = try_qualify(expression, ctx.dialect, ctx.schema)
        if qualified is None:
            return expression, []
        ctx.qualified = True
        out: list[Rewrite] = []
        before_selects = list(expression.find_all(exp.Select))
        after_selects = list(qualified.find_all(exp.Select))
        if len(before_selects) == len(after_selects):
            for old, new in zip(before_selects, after_selects, strict=True):
                stars = [s for s in _select_stars(old) if s.find_ancestor(exp.Select) is old]
                if not stars or _select_stars(new):
                    continue
                cols = [e.alias_or_name for e in new.expressions]
                out.append(self.rewrite(", ".join(s.sql(dialect=ctx.dialect) for s in stars),
                                        ", ".join(cols),
                                        f"SELECT * expanded to {len(cols)} column(s) from the declared schema.",
                                        ctx, node=stars[0]))
        return qualified, out
