"""Catalog of every *finding* rule (as opposed to the optimizer's rewrite rules, which already
have their own registry in :mod:`sqlanalyzer.optimizer.rules` and GUI dialog).

The static/partition/engine/schema/plan analyzers in this package each hard-code their own list
of rule names; this module gathers them into one flat, describable list so the GUI can offer a
single "disable these checks" dialog (:mod:`sqlanalyzer.gui.rules_dialog`) filterable by category
or engine, mirroring the optimizer's ``RulesDialog``. It intentionally does not change *how* a
rule fires - :func:`filter_findings` just drops findings by name after the fact, so enabling a
rule again later needs no re-analysis beyond the usual one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .dialects import Engine
from .models import Category, Finding
from .static_engine import ENGINE_RULES

ALL_ENGINES = frozenset(Engine)


@dataclass(frozen=True)
class RuleInfo:
    name: str
    category: Category
    description: str
    engines: frozenset[Engine] = ALL_ENGINES  # rules that only fire for some engines


# ---------------------------------------------------------------------- static.py (lint/join/scan)
_STATIC_RULES = [
    RuleInfo("missing-where", Category.LINT, "DELETE/UPDATE without a WHERE clause affects every row."),
    RuleInfo("select-star", Category.LINT, "SELECT * reads every column instead of just the ones needed."),
    RuleInfo("null-comparison", Category.LINT, "Comparing to NULL with =/<> instead of IS [NOT] NULL."),
    RuleInfo("not-in-subquery", Category.LINT, "NOT IN (subquery) silently returns no rows if it yields a NULL."),
    RuleInfo("union-distinct", Category.LINT, "UNION (instead of UNION ALL) forces a full de-duplication."),
    RuleInfo("order-by-ordinal", Category.LINT, "ORDER BY references a column by position."),
    RuleInfo("order-by-in-subquery", Category.LINT, "ORDER BY in a subquery/CTE without LIMIT is usually wasted."),
    RuleInfo("limit-without-order", Category.LINT, "LIMIT without ORDER BY returns an arbitrary set of rows."),
    RuleInfo("having-without-aggregate", Category.LINT, "HAVING predicate has no aggregate; belongs in WHERE."),
    RuleInfo("leading-wildcard", Category.SCAN, "LIKE pattern starts with a wildcard; indexes can't be used."),
    RuleInfo("many-joins", Category.JOIN, "A query block joins an unusually large number of relations."),
    RuleInfo("implicit-join", Category.JOIN, "Comma/CROSS JOIN linked only through a WHERE predicate."),
    RuleInfo("cartesian-join", Category.JOIN, "Join with no condition at all produces a Cartesian product."),
    RuleInfo("left-join-filtered", Category.JOIN, "WHERE filters the outer side, turning a LEFT JOIN into INNER."),
    RuleInfo("correlated-subquery", Category.JOIN, "Correlated scalar subquery in the SELECT list, per outer row."),
    RuleInfo("non-equi-join", Category.JOIN, "JOIN has no equality condition between the two sides."),
    RuleInfo("join-on-expression", Category.JOIN, "Join key is wrapped in a function/cast/arithmetic expression."),
    RuleInfo("or-in-join", Category.JOIN, "OR inside a JOIN condition usually prevents hash joins."),
    RuleInfo("non-sargable-predicate", Category.SCAN, "A filtered column is wrapped in an expression."),
    RuleInfo("unfiltered-scan", Category.SCAN, "No WHERE clause: the table will be read in full."),
    RuleInfo("or-across-columns", Category.SCAN, "OR across different columns often prevents pruning."),
    RuleInfo("repeated-scan", Category.SCAN, "The same table is scanned more than once in one statement."),
]

# ----------------------------------------------------------------- static_schema.py (needs a schema)
_SCHEMA_RULES = [
    RuleInfo("unknown-table", Category.SCHEMA, "Table referenced by the query is not in the declared schema."),
    RuleInfo("unknown-column", Category.SCHEMA, "Column referenced by the query does not exist on its table."),
    RuleInfo("unknown-alias", Category.SCHEMA, "Column is qualified by an alias that is not in scope."),
    RuleInfo("ambiguous-column", Category.SCHEMA, "Unqualified column exists on more than one joined table."),
    RuleInfo("join-type-mismatch", Category.SCHEMA, "Join keys have incompatible/implicitly-cast types."),
    RuleInfo("predicate-type-mismatch", Category.SCHEMA, "A predicate compares a column to an incompatible literal."),
    RuleInfo("join-on-unindexed-column", Category.SCHEMA, "Join key on a large table has no index/key declared."),
    RuleInfo("wide-select-star", Category.SCHEMA, "SELECT * expands to an unusually large number of columns."),
]

# ------------------------------------------------------------------------------- partition.py
_PARTITION_RULES = [
    RuleInfo("partition-column-wrapped", Category.PARTITION,
            "Predicate on a partition column wraps it in a function/cast; pruning is lost."),
    RuleInfo("partition-like-wildcard", Category.PARTITION,
            "Leading-wildcard LIKE on a partition column cannot prune partitions."),
    RuleInfo("partition-dynamic-filter", Category.PARTITION,
            "Partition column is filtered by a non-constant expression."),
    RuleInfo("partition-or-filter", Category.PARTITION,
            "Partition-column filter sits inside an OR with other columns; pruning is likely lost."),
    RuleInfo("missing-partition-filter", Category.PARTITION,
            "A declared partition column has no filter at all; every partition will be scanned."),
    RuleInfo("possible-missing-partition-filter", Category.PARTITION,
            "A column that looks like a partition key is not filtered."),
]

# ------------------------------------------------------------------------------ complexity.py
_COMPLEXITY_RULES = [
    RuleInfo("high-complexity", Category.COMPLEXITY, "The query's structural complexity score is high."),
]

# -------------------------------------------------------------------------- plans/analysis.py
_PLAN_RULES = [
    RuleInfo("cartesian-product", Category.PLAN, "A nested-loop join has no condition: a Cartesian product."),
    RuleInfo("expensive-nested-loop", Category.PLAN, "A nested-loop join runs over a large number of rows."),
    RuleInfo("function-in-scan-filter", Category.PLAN, "A scan's filter wraps the column; pruning/index is lost."),
    RuleInfo("hash-spill", Category.PLAN, "A hash operator spilled to disk (too little memory for its input)."),
    RuleInfo("hot-operator", Category.PLAN, "A single operator accounts for most of the query's time/cost."),
    RuleInfo("index-only-heap-fetches", Category.PLAN, "An index-only scan still had to fetch heap pages."),
    RuleInfo("join-row-explosion", Category.PLAN, "A join emits far more rows than either side had."),
    RuleInfo("large-seq-scan", Category.PLAN, "A sequential/full scan reads a large number of rows."),
    RuleInfo("large-unfiltered-scan", Category.PLAN, "A large scan has no filter at all."),
    RuleInfo("no-partition-pruning", Category.PLAN, "An append/union over partitions pruned none of them."),
    RuleInfo("partition-filter-not-pushed", Category.PLAN, "A partition filter did not reach the scan operator."),
    RuleInfo("poor-partition-pruning", Category.PLAN, "Partition pruning removed only a small share of partitions."),
    RuleInfo("row-misestimate", Category.PLAN, "The planner's row estimate is far from the actual row count."),
    RuleInfo("scan-discards-rows", Category.PLAN, "A scan's filter discards most of the rows it reads."),
    RuleInfo("sort-spill", Category.PLAN, "A sort operator spilled to disk."),
]

ALL_RULES: list[RuleInfo] = [
    *_STATIC_RULES,
    *_SCHEMA_RULES,
    *_PARTITION_RULES,
    *_COMPLEXITY_RULES,
    *_PLAN_RULES,
    *(RuleInfo(r.name, Category.ENGINE, r.description, r.engines) for r in ENGINE_RULES),
]
_BY_NAME = {r.name: r for r in ALL_RULES}


def rule_info(name: str) -> RuleInfo | None:
    return _BY_NAME.get(name)


def rules_by_category() -> dict[Category, list[RuleInfo]]:
    out: dict[Category, list[RuleInfo]] = {}
    for rule in ALL_RULES:
        out.setdefault(rule.category, []).append(rule)
    return out


def filter_findings(findings: list[Finding], disabled: Iterable[str]) -> list[Finding]:
    """Drop findings whose rule name is in ``disabled`` (unknown names are ignored)."""
    disabled = set(disabled)
    if not disabled:
        return findings
    return [f for f in findings if f.rule not in disabled]


__all__ = ["ALL_RULES", "RuleInfo", "filter_findings", "rule_info", "rules_by_category"]
