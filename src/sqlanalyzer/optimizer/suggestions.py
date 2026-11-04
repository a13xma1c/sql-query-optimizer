"""Individual rewrite suggestions: one rule at a time, each with its own resulting SQL.

The optimizer applies every rule at once. Users often want to pick: accept the
sargable-date rewrite but keep ``UNION`` as it is. :func:`suggest` runs each
rule in isolation on the original query and returns a :class:`Suggestion` per
rule that changes something, including the complete SQL *with only that rule
applied*. :func:`apply_rules` applies a chosen subset together.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from .engine import QUALIFY_RULE, OptimizationResult, Optimizer, Rewrite, Rule, Safety
from .rules import default_rules, rule_by_name


@dataclass
class Suggestion:
    rule: str
    title: str
    description: str  # rule description (why it is faster)
    category: str
    safety: Safety
    rewrites: list[Rewrite]  # the concrete changes this rule makes to the query
    sql: str  # the whole query with only this rule applied
    fixes: tuple[str, ...] = ()
    needs_qualification: bool = False

    @property
    def line(self) -> int | None:
        return next((rw.line for rw in self.rewrites if rw.line), None)

    @property
    def summary(self) -> str:
        first = self.rewrites[0]
        extra = f" (+{len(self.rewrites) - 1} more)" if len(self.rewrites) > 1 else ""
        return f"{first.before} -> {first.after}{extra}"

    def addresses(self, finding_rule: str) -> bool:
        return finding_rule in self.fixes


def suggest(statements: Sequence[exp.Expr], dialect: str, *, schema: Mapping[str, Any] | None = None,
            rules: Sequence[Rule] | None = None, include_review: bool = True,
            disabled: Iterable[str] = ()) -> list[Suggestion]:
    """One suggestion per rule that would change the query (rules run independently)."""
    disabled = set(disabled)
    candidates = [r for r in (rules if rules is not None else default_rules()) if r.name != QUALIFY_RULE]
    qualifier = rule_by_name(QUALIFY_RULE)
    out: list[Suggestion] = []
    for rule in candidates:
        if rule.name in disabled or (rule.engines is not None and dialect not in rule.engines):
            continue
        if rule.safety is Safety.REVIEW and not include_review:
            continue
        chain = [qualifier, rule] if (rule.requires_qualified or schema) else [rule]
        engine = Optimizer(chain, include_review=include_review, max_passes=2)
        result = engine.optimize(statements, dialect, schema)
        mine = [rw for rw in result.rewrites if rw.rule == rule.name]
        if not mine:
            continue
        safety = Safety.REVIEW if any(rw.safety is Safety.REVIEW for rw in mine) else Safety.SAFE
        out.append(Suggestion(rule.name, rule.title, rule.description.replace("``", ""), rule.category,
                              safety, mine, result.optimized_sql, rule.fixes, rule.requires_qualified))
    return out


def apply_rules(statements: Sequence[exp.Expr], dialect: str, names: Iterable[str], *,
                schema: Mapping[str, Any] | None = None, include_review: bool = True) -> OptimizationResult:
    """Apply only the named rules (in the default order, with the usual passes)."""
    wanted = set(names)
    rules = [r for r in default_rules(include_opt_in=True) if r.name in wanted or r.name == QUALIFY_RULE]
    return Optimizer(rules, include_review=include_review).optimize(statements, dialect, schema)


def suggestions_for(suggestions: Iterable[Suggestion], finding_rule: str) -> list[Suggestion]:
    return [s for s in suggestions if s.addresses(finding_rule)]


__all__ = ["Suggestion", "apply_rules", "suggest", "suggestions_for"]
