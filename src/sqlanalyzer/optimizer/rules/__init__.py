"""Built-in rewrite rules, in the order the engine applies them."""

from __future__ import annotations

from ..engine import Rule
from .grouping import HavingToWhereRule, RedundantDistinctRule, UnionAllRule
from .joins import ExplicitJoinRule, LeftJoinToInnerRule, RedundantJoinConditionRule
from .naming import QualifyRule
from .predicates import (
    LikeWithoutWildcardRule,
    NullComparisonRule,
    OrToInRule,
    PrefixMatchRule,
    SargableDateRule,
    SimplifyRule,
    SingleValueInRule,
)
from .sqlglot_passes import (
    SQLGLOT_PASS_RULES,
    EliminateJoinsRule,
    EliminateSubqueriesRule,
    MergeSubqueriesRule,
    OptimizeJoinsRule,
    PushdownPredicatesRule,
    PushdownProjectionsRule,
    UnnestSubqueriesRule,
)
from .subqueries import (
    DistinctInSemiJoinRule,
    ExistsProjectionRule,
    NotInToNotExistsRule,
    SubqueryOrderByRule,
    UnusedCteRule,
)

RULE_CLASSES: list[type[Rule]] = [
    QualifyRule,
    # local, schema-free rewrites first: cheap and they make the later passes more effective
    UnusedCteRule,
    NullComparisonRule,
    SimplifyRule,
    SargableDateRule,
    PrefixMatchRule,
    LikeWithoutWildcardRule,
    SingleValueInRule,
    OrToInRule,
    ExplicitJoinRule,
    LeftJoinToInnerRule,
    RedundantJoinConditionRule,
    HavingToWhereRule,
    RedundantDistinctRule,
    DistinctInSemiJoinRule,
    ExistsProjectionRule,
    SubqueryOrderByRule,
    NotInToNotExistsRule,
    UnionAllRule,
    # SQLGlot scope-aware passes (need qualified columns)
    *SQLGLOT_PASS_RULES,
]

# Available but not applied unless explicitly enabled: the output is correct but verbose.
OPT_IN_RULES: frozenset[str] = frozenset({UnnestSubqueriesRule.name})


def default_rules(include_opt_in: bool = False) -> list[Rule]:
    return [cls() for cls in RULE_CLASSES if include_opt_in or cls.name not in OPT_IN_RULES]


def rule_by_name(name: str) -> Rule:
    for cls in RULE_CLASSES:
        if cls.name == name:
            return cls()
    raise KeyError(name)


__all__ = [
    "RULE_CLASSES",
    "OPT_IN_RULES",
    "default_rules",
    "rule_by_name",
    "QualifyRule",
    "UnusedCteRule",
    "NullComparisonRule",
    "SimplifyRule",
    "SargableDateRule",
    "PrefixMatchRule",
    "LikeWithoutWildcardRule",
    "SingleValueInRule",
    "OrToInRule",
    "ExplicitJoinRule",
    "LeftJoinToInnerRule",
    "RedundantJoinConditionRule",
    "HavingToWhereRule",
    "RedundantDistinctRule",
    "DistinctInSemiJoinRule",
    "ExistsProjectionRule",
    "SubqueryOrderByRule",
    "NotInToNotExistsRule",
    "UnionAllRule",
    "PushdownPredicatesRule",
    "PushdownProjectionsRule",
    "MergeSubqueriesRule",
    "EliminateSubqueriesRule",
    "EliminateJoinsRule",
    "OptimizeJoinsRule",
    "UnnestSubqueriesRule",
]
