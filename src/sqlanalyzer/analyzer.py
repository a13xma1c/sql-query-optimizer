"""High-level facade combining every analysis into one result."""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.errors import SqlglotError

from .ast import AstNode, build_ast
from .complexity import ComplexityReport, analyze_complexity
from .dialects import ENGINES, Engine
from .models import Category, Finding, sort_findings
from .optimizer import OptimizationResult, optimize_query
from .parsing import format_sql, parse_sql
from .partition import PartitionHints, analyze_partitions
from .plans import Plan, PlanParseError, parse_plan
from .plans.analysis import analyze_plan
from .plans.diff import PlanDiff, diff_plans
from .static import analyze_static

log = logging.getLogger(__name__)


@dataclass
class QueryAnalysis:
    sql: str
    engine: Engine
    syntax_ok: bool = True
    findings: list[Finding] = field(default_factory=list)
    complexity: ComplexityReport | None = None
    formatted: str | None = None
    plan: Plan | None = None
    statements: list[exp.Expression] = field(default_factory=list)  # SQLGlot ASTs
    ast: list[AstNode] = field(default_factory=list)  # one tree per statement
    optimization: OptimizationResult | None = None
    optimized_findings: list[Finding] = field(default_factory=list)  # static findings of the rewritten query

    def by_category(self, category) -> list[Finding]:
        return [f for f in self.findings if f.category == category]

    @property
    def resolved_by_optimization(self) -> list[Finding]:
        """Findings of the original query that disappear in the optimized version."""
        if self.optimization is None or not self.optimization.changed:
            return []
        remaining = {f.key for f in self.optimized_findings}
        static = {Category.SYNTAX, Category.PLAN, Category.COMPLEXITY}
        return [f for f in self.findings if f.category not in static and f.key not in remaining]


def analyze_query(sql: str, engine: Engine, partition_hints: PartitionHints | None = None,
                  plan_text: str | None = None, *, schema: Mapping[str, Any] | None = None,
                  optimize: bool = True, include_review: bool = True,
                  disabled_rules: Iterable[str] = ()) -> QueryAnalysis:
    """Run syntax, lint, join/scan, partition, complexity and optimization analysis.

    If ``plan_text`` is supplied it is parsed and the plan-level rules are added.
    ``schema`` (``{table: {column: type}}``) enables star expansion and the
    scope-aware optimizer passes.
    """
    dialect = ENGINES[engine].sqlglot_dialect
    result = QueryAnalysis(sql, engine)
    parsed = parse_sql(sql, dialect)
    findings: list[Finding] = list(parsed.errors)
    if parsed.ok:
        result.statements = parsed.statements
        result.ast = [build_ast(s, dialect) for s in parsed.statements]
        findings += analyze_static(parsed.statements)
        findings += analyze_partitions(parsed.statements, partition_hints)
        result.complexity = analyze_complexity(parsed.statements)
        findings += result.complexity.findings()
        try:
            result.formatted = format_sql(sql, dialect)
        except SqlglotError:  # pragma: no cover - parsing already succeeded
            result.formatted = None
        if optimize:
            run_optimization(result, schema=schema, partition_hints=partition_hints, include_review=include_review,
                             disabled_rules=disabled_rules)
    else:
        result.syntax_ok = False

    if plan_text:
        try:
            result.plan = parse_plan(engine, plan_text)
            findings += analyze_plan(result.plan)
        except PlanParseError as exc:
            from .models import Severity
            findings.append(Finding("plan-parse-error", Severity.ERROR, Category.PLAN, str(exc)))

    result.findings = sort_findings(_dedupe(findings))
    return result


def run_optimization(result: QueryAnalysis, *, schema: Mapping[str, Any] | None = None,
                     partition_hints: PartitionHints | None = None, include_review: bool = True,
                     disabled_rules: Iterable[str] = ()) -> OptimizationResult | None:
    """Run the rule engine for an already parsed analysis and attach the outcome.

    Separate from :func:`analyze_query` so a GUI can run it off the main thread:
    it only reads ``result.statements`` (the engine works on copies).
    """
    if not result.syntax_ok or not result.statements:
        return None
    dialect = ENGINES[result.engine].sqlglot_dialect
    try:
        optimization = optimize_query(result.statements, dialect, schema=schema, include_review=include_review,
                                      disabled=disabled_rules)
    except Exception:  # noqa: BLE001 - the optimizer must never break the analysis
        log.exception("optimizer failed")
        return None
    optimized_findings: list[Finding] = []
    if optimization.changed:
        optimized = optimization.statements
        optimized_findings = sort_findings(_dedupe(
            analyze_static(optimized) + analyze_partitions(optimized, partition_hints)))
    result.optimization = optimization
    result.optimized_findings = optimized_findings
    return optimization


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple] = set()
    out = []
    for f in findings:
        key = (f.rule, f.message, f.line, f.col)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


@dataclass
class Comparison:
    old: QueryAnalysis
    new: QueryAnalysis
    plan_diff: PlanDiff | None
    resolved: list[Finding]
    introduced: list[Finding]
    complexity_delta: int | None


def compare_queries(old: QueryAnalysis, new: QueryAnalysis) -> Comparison:
    old_keys = {f.key for f in old.findings}
    new_keys = {f.key for f in new.findings}
    resolved = [f for f in old.findings if f.key not in new_keys]
    introduced = [f for f in new.findings if f.key not in old_keys]
    diff = diff_plans(old.plan, new.plan) if old.plan and new.plan else None
    delta = (new.complexity.score - old.complexity.score) if old.complexity and new.complexity else None
    return Comparison(old, new, diff, resolved, introduced, delta)
