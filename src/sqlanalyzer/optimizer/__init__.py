"""Rule-based query optimization on SQLGlot ASTs.

Usage::

    from sqlanalyzer.optimizer import optimize_query
    result = optimize_query("SELECT * FROM a, b WHERE a.id = b.a_id AND a.x = NULL", "postgres")
    print(result.optimized_sql)
    for rw in result.rewrites:
        print(rw.rule, rw.safety, rw.description)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from sqlglot import exp

from ..parsing import parse_sql
from .engine import MAX_PASSES, OptimizationResult, Optimizer, Rewrite, Rule, RuleContext, Safety
from .rules import OPT_IN_RULES, RULE_CLASSES, default_rules, rule_by_name
from .schema import SchemaHints, TableStats, format_schema, parse_schema
from .suggestions import Suggestion, apply_rules, suggest, suggestions_for


def optimize_query(sql: str | Sequence[exp.Expr], dialect: str, *, schema: Mapping[str, Any] | None = None,
                   rules: Sequence[Rule] | None = None, include_review: bool = True,
                   disabled: Iterable[str] = (), max_passes: int = MAX_PASSES) -> OptimizationResult:
    """Optimize ``sql`` (text or already parsed statements) for ``dialect``.

    Raises ``ValueError`` if the SQL text does not parse.
    """
    if isinstance(sql, str):
        parsed = parse_sql(sql, dialect)
        if not parsed.ok:
            raise ValueError("; ".join(e.message for e in parsed.errors))
        statements: Sequence[exp.Expr] = parsed.statements
    else:
        statements = sql
    engine = Optimizer(rules if rules is not None else default_rules(), include_review=include_review,
                       disabled=disabled, max_passes=max_passes)
    return engine.optimize(statements, dialect, schema)


__all__ = [
    "MAX_PASSES",
    "OptimizationResult",
    "Optimizer",
    "Rewrite",
    "Rule",
    "RuleContext",
    "Safety",
    "RULE_CLASSES",
    "OPT_IN_RULES",
    "SchemaHints",
    "Suggestion",
    "TableStats",
    "apply_rules",
    "suggest",
    "suggestions_for",
    "default_rules",
    "format_schema",
    "optimize_query",
    "parse_schema",
    "rule_by_name",
]
