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
from .indexes import IndexRecommendation, recommend_indexes
from .models import Category, Finding, sort_findings
from .optimizer import OptimizationResult, Suggestion, TableStats, optimize_query, suggest
from .parsing import format_sql, parse_sql
from .partition import PartitionHints, analyze_partitions
from .plans import Plan, PlanParseError, parse_plan
from .plans.analysis import analyze_plan
from .plans.costing import CostEstimate, cost_delta, estimate_cost
from .plans.diff import PlanDiff, diff_plans
from .plans.explain import PlanExplanation, explain_plan
from .plans.logical import build_logical_plan
from .rules_registry import filter_findings
from .static import analyze_static
from .static_engine import analyze_engine_specific
from .static_schema import analyze_schema_aware

log = logging.getLogger(__name__)


@dataclass
class QueryAnalysis:
    sql: str
    engine: Engine
    syntax_ok: bool = True
    findings: list[Finding] = field(default_factory=list)
    complexity: ComplexityReport | None = None
    formatted: str | None = None
    plan: Plan | None = None  # parsed from EXPLAIN output (needs a database or a pasted plan)
    logical_plan: Plan | None = None  # derived from the SQL itself, always available
    explanation: PlanExplanation | None = None  # step-by-step description of the effective plan
    statements: list[exp.Expr] = field(default_factory=list)  # SQLGlot ASTs
    ast: list[AstNode] = field(default_factory=list)  # one tree per statement
    optimization: OptimizationResult | None = None
    optimized_findings: list[Finding] = field(default_factory=list)  # static findings of the rewritten query
    suggestions: list[Suggestion] = field(default_factory=list)  # one per rule, individually applicable
    cost: CostEstimate | None = None  # offline cost model of the query as written
    optimized_cost: CostEstimate | None = None  # ... and of the rule engine's rewrite
    index_recommendations: list[IndexRecommendation] = field(default_factory=list)
    cost_with_indexes: CostEstimate | None = None  # rewrite (or original) once the recommended indexes exist

    def by_category(self, category) -> list[Finding]:
        return [f for f in self.findings if f.category == category]

    @property
    def effective_plan(self) -> Plan | None:
        """The engine's plan when one was loaded, otherwise the offline (logical) plan."""
        return self.plan or self.logical_plan

    @property
    def resolved_by_optimization(self) -> list[Finding]:
        """Findings of the original query that disappear in the optimized version."""
        if self.optimization is None or not self.optimization.changed:
            return []
        remaining = {f.key for f in self.optimized_findings}
        static = {Category.SYNTAX, Category.PLAN, Category.COMPLEXITY}
        return [f for f in self.findings if f.category not in static and f.key not in remaining]

    @property
    def cost_change(self) -> str | None:
        """``"2.4M -> 745K cost units (-70%)"`` when the rewrite changes the estimated cost."""
        return cost_delta(self.cost, self.optimized_cost)

    @property
    def cost_change_with_indexes(self) -> str | None:
        return cost_delta(self.cost, self.cost_with_indexes)

    def suggestions_for(self, finding: Finding) -> list[Suggestion]:
        return [s for s in self.suggestions if s.addresses(finding.rule)]


def analyze_query(sql: str, engine: Engine, partition_hints: PartitionHints | None = None,
                  plan_text: str | None = None, *, schema: Mapping[str, Any] | None = None,
                  stats: Mapping[str, TableStats] | None = None, optimize: bool = True,
                  include_review: bool = True, disabled_rules: Iterable[str] = (),
                  disabled_finding_rules: Iterable[str] = ()) -> QueryAnalysis:
    """Run syntax, lint, join/scan, partition, engine, schema, complexity, cost and optimization analysis.

    If ``plan_text`` is supplied it is parsed and the plan-level rules are added.
    ``schema`` (``{table: {column: type}}``) enables the schema-aware rules
    (unknown/ambiguous columns, join-key and literal type checks), star expansion
    and the scope-aware optimizer passes; ``stats`` (row counts, keys, indexes)
    feed the offline cost model, the index advisor and the engine-specific rules.
    ``disabled_rules`` silences optimizer *rewrite* rules (see :mod:`sqlanalyzer.optimizer.rules`);
    ``disabled_finding_rules`` silences *finding* rules by name (see :mod:`sqlanalyzer.rules_registry`),
    e.g. ones a team has decided are noise for their codebase.
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
        findings += analyze_engine_specific(parsed.statements, engine, stats)
        findings += analyze_schema_aware(parsed.statements, schema, stats)
        result.complexity = analyze_complexity(parsed.statements)
        findings += result.complexity.findings()
        try:
            result.formatted = format_sql(sql, dialect)
        except SqlglotError:  # pragma: no cover - parsing already succeeded
            result.formatted = None
        result.logical_plan = _logical_plan(parsed.statements, engine)
        result.cost = _estimate(result.logical_plan, parsed.statements, engine, stats)
    else:
        result.syntax_ok = False

    if plan_text:
        try:
            result.plan = parse_plan(engine, plan_text)
            findings += analyze_plan(result.plan)
        except PlanParseError as exc:
            from .models import Severity
            findings.append(Finding("plan-parse-error", Severity.ERROR, Category.PLAN, str(exc)))

    result.findings = filter_findings(sort_findings(_dedupe(findings)), disabled_finding_rules)
    if result.effective_plan is not None:
        try:
            result.explanation = explain_plan(result.effective_plan, result.findings)
        except Exception:  # noqa: BLE001 - an explanation must never break the analysis
            log.exception("plan explanation failed")
    if parsed.ok:
        try:
            result.index_recommendations = recommend_indexes(parsed.statements, engine, stats,
                                                             result.effective_plan, dialect)
            result.cost_with_indexes = _cost_with_indexes(result, stats)
        except Exception:  # noqa: BLE001 - advice must never break the analysis
            log.exception("index advisor failed")
        if optimize:
            run_optimization(result, schema=schema, stats=stats, partition_hints=partition_hints,
                             include_review=include_review, disabled_rules=disabled_rules,
                             disabled_finding_rules=disabled_finding_rules)
    return result


def _cost_with_indexes(result: QueryAnalysis, stats: Mapping[str, TableStats] | None) -> CostEstimate | None:
    """Cost of the best available SQL (rewrite if any) assuming the recommended indexes exist."""
    if not result.index_recommendations:
        return None
    augmented: dict[str, TableStats] = {k.lower(): v for k, v in (stats or {}).items()}
    for rec in result.index_recommendations:
        if rec.kind == "expression":
            continue
        key = rec.table.lower()
        match = next((k for k in augmented if k == key or k.split(".")[-1] == key.split(".")[-1]), key)
        current = augmented.get(match, TableStats())
        augmented[match] = TableStats(current.rows, current.unique, current.ndv, current.columns,
                                      current.indexed | frozenset(c.lower() for c in rec.columns))
    statements = result.optimization.statements if result.optimization and result.optimization.changed \
        else result.statements
    plan = _logical_plan(statements, result.engine)
    return _estimate(plan, statements, result.engine, augmented)


def _estimate(plan: Plan | None, statements: list[exp.Expr], engine: Engine,
              stats: Mapping[str, TableStats] | None) -> CostEstimate | None:
    if plan is None:
        return None
    try:
        return estimate_cost(plan, engine, stats, statements[0] if statements else None)
    except Exception:  # noqa: BLE001 - the model must never break the analysis
        log.exception("cost model failed")
        return None


def _logical_plan(statements: list[exp.Expr], engine: Engine) -> Plan | None:
    """Plan derived from the SQL text (first statement); never raises."""
    if not statements:
        return None
    try:
        return build_logical_plan(statements[0], engine)
    except Exception:  # noqa: BLE001 - exotic syntax must not break the analysis
        log.exception("logical plan failed")
        return None


def run_optimization(result: QueryAnalysis, *, schema: Mapping[str, Any] | None = None,
                     stats: Mapping[str, TableStats] | None = None,
                     partition_hints: PartitionHints | None = None, include_review: bool = True,
                     disabled_rules: Iterable[str] = (),
                     disabled_finding_rules: Iterable[str] = ()) -> OptimizationResult | None:
    """Run the rule engine (and the per-rule suggestions) for an already parsed analysis.

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
    optimized_cost: CostEstimate | None = None
    if optimization.changed:
        optimized = optimization.statements
        optimized_findings = filter_findings(sort_findings(_dedupe(
            analyze_static(optimized) + analyze_partitions(optimized, partition_hints)
            + analyze_engine_specific(optimized, result.engine, stats))), disabled_finding_rules)
        optimized_cost = _estimate(_logical_plan(optimized, result.engine), optimized, result.engine, stats)
    try:
        suggestions = suggest(result.statements, dialect, schema=schema, include_review=include_review,
                              disabled=disabled_rules)
    except Exception:  # noqa: BLE001
        log.exception("suggestions failed")
        suggestions = []
    result.optimization = optimization
    result.optimized_findings = optimized_findings
    result.optimized_cost = optimized_cost
    result.suggestions = suggestions
    if optimization.changed and result.index_recommendations:
        try:
            result.cost_with_indexes = _cost_with_indexes(result, stats)
        except Exception:  # noqa: BLE001
            log.exception("cost with indexes failed")
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
    offline_plans: bool = False  # the diff compares the plans derived from the SQL, not engine plans
    cost_change: str | None = None  # offline cost model: old -> new


def compare_queries(old: QueryAnalysis, new: QueryAnalysis) -> Comparison:
    """Diff two analyses; execution plans are compared offline when none was loaded."""
    old_keys = {f.key for f in old.findings}
    new_keys = {f.key for f in new.findings}
    resolved = [f for f in old.findings if f.key not in new_keys]
    introduced = [f for f in new.findings if f.key not in old_keys]
    old_plan, new_plan = old.effective_plan, new.effective_plan
    diff = diff_plans(old_plan, new_plan) if old_plan and new_plan else None
    offline = bool(old_plan and new_plan and (old_plan.logical or new_plan.logical))
    delta = (new.complexity.score - old.complexity.score) if old.complexity and new.complexity else None
    return Comparison(old, new, diff, resolved, introduced, delta, offline, cost_delta(old.cost, new.cost))
