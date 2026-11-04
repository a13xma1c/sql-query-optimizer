"""Validate an LLM rewrite against the deterministic rule set and the original query.

A local model is a suggestion engine, not an oracle: it can hallucinate tables,
silently drop a filter or "optimize" a query into something that returns other
rows. Before anything is shown as applicable, the rewrite is checked by the same
machinery the rest of the app uses:

* it must parse in the dialect and keep the statement shape,
* it may only reference sources of the original query (or the declared schema),
* projections, predicates, grouping, DISTINCT and LIMIT must survive,
* the static analyzer must not report *new* problems,
* the rule engine is run on the model's SQL: rules that still fire are
  optimizations the model missed, and a rewrite identical to the rule engine's
  output is reported as fully corroborated.

The result is a list of :class:`Check` objects with ``pass`` / ``warn`` / ``fail``.
A single ``fail`` makes the suggestion non-applicable in the GUI.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from ..models import Finding, Severity
from ..optimizer import OptimizationResult, Safety, optimize_query
from ..optimizer.engine import canonical_sql
from ..parsing import parse_sql
from ..partition import PartitionHints, analyze_partitions
from ..static import analyze_static

PASS, WARN, FAIL = "pass", "warn", "fail"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass
class LlmReview:
    checks: list[Check] = field(default_factory=list)
    introduced: list[Finding] = field(default_factory=list)  # new static findings
    resolved: list[Finding] = field(default_factory=list)  # findings the rewrite removes
    missed_rules: list[str] = field(default_factory=list)  # rule rewrites still applicable afterwards
    matches_rules: bool = False  # identical (up to operand order) to the rule engine's rewrite
    statements: list[exp.Expr] = field(default_factory=list)  # parsed LLM statements

    # ------------------------------------------------------------------ verdict
    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def verdict(self) -> str:
        if self.failures:
            return "rejected"
        if self.warnings:
            return "needs review"
        return "validated"

    def summary(self) -> str:
        n_pass = sum(c.status == PASS for c in self.checks)
        text = f"{self.verdict.capitalize()}: {n_pass}/{len(self.checks)} checks passed"
        if self.failures:
            text += f", {len(self.failures)} failed"
        if self.warnings:
            text += f", {len(self.warnings)} warning(s)"
        if self.matches_rules:
            text += ". The rewrite matches the rule engine's result"
        elif self.missed_rules:
            text += f". Rules that still apply afterwards: {', '.join(self.missed_rules)}"
        return text + "."


def review_llm_sql(original_sql: str, llm_sql: str, dialect: str, *,
                   schema: Mapping[str, Any] | None = None, partition_hints: PartitionHints | None = None,
                   rules: OptimizationResult | None = None, declared_changes: Iterable[dict] = ()) -> LlmReview:
    """Check ``llm_sql`` against ``original_sql`` and the rule engine."""
    review = LlmReview()
    original = parse_sql(original_sql, dialect)
    candidate = parse_sql(llm_sql, dialect)

    if not candidate.ok:
        detail = "; ".join(f.message for f in candidate.errors)
        review.checks.append(Check("syntax", FAIL, f"The rewrite does not parse as {dialect}: {detail}"))
        return review
    review.statements = candidate.statements
    review.checks.append(Check("syntax", PASS, f"Parses as valid {dialect} SQL."))
    if not original.ok:  # pragma: no cover - the caller only optimizes valid queries
        review.checks.append(Check("baseline", FAIL, "The original query does not parse; nothing to compare with."))
        return review

    review.checks.append(_statement_check(original.statements, candidate.statements))
    review.checks.append(_source_check(original.statements, candidate.statements, schema))
    review.checks.append(_projection_check(original.statements, candidate.statements, schema))
    review.checks.append(_predicate_check(original.statements, candidate.statements, dialect))
    if rules is None:
        try:
            rules = optimize_query(original.statements, dialect, schema=schema)
        except Exception:  # noqa: BLE001 - the rule engine is only a reference here
            rules = None
    review.checks.append(_shape_check(original.statements, candidate.statements,
                                      rules.statements if rules is not None else None))
    review.checks.append(_identity_check(original.statements, candidate.statements, dialect))
    review.checks.append(_findings_check(original.statements, candidate.statements, partition_hints, review))
    review.checks.append(_rules_check(candidate.statements, dialect, schema, rules, review))
    risky = [c for c in declared_changes if str(c.get("risk", "")).lower() not in ("", "none", "no", "safe")]
    if risky:
        titles = ", ".join(str(c.get("title") or "change") for c in risky)
        review.checks.append(Check("declared-risk", WARN,
                                   f"The model flagged {len(risky)} change(s) as possibly result-changing: {titles}."))
    return review


# ----------------------------------------------------------------------- checks
def _statement_check(old: list[exp.Expr], new: list[exp.Expr]) -> Check:
    if len(old) != len(new):
        return Check("statements", FAIL, f"The original has {len(old)} statement(s), the rewrite {len(new)}.")
    old_kinds = [type(s).__name__ for s in old]
    new_kinds = [type(s).__name__ for s in new]
    if old_kinds != new_kinds:
        return Check("statements", FAIL, f"Statement types changed: {', '.join(old_kinds)} -> {', '.join(new_kinds)}.")
    return Check("statements", PASS, f"Same {len(old)} statement(s) of the same type.")


def _tables(statements: list[exp.Expr]) -> set[str]:
    names: set[str] = set()
    ctes = {c.alias.lower() for s in statements for c in s.find_all(exp.CTE)}
    for statement in statements:
        for table in statement.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                continue
            name = ".".join(p for p in (table.catalog, table.db, table.name) if p).lower()
            if table.name.lower() in ctes and not table.db:
                continue
            names.add(name)
    return names


def _source_check(old: list[exp.Expr], new: list[exp.Expr],
                  schema: Mapping[str, Any] | None) -> Check:
    before, after = _tables(old), _tables(new)
    known = set(before) | _schema_tables(schema)
    invented = {t for t in after if t not in known and t.split(".")[-1] not in {k.split(".")[-1] for k in known}}
    if invented:
        return Check("sources", FAIL, "The rewrite reads table(s) that are neither in the query nor in the declared "
                                      f"schema: {', '.join(sorted(invented))}.")
    dropped = before - after
    if dropped:
        return Check("sources", WARN, f"Table(s) no longer read: {', '.join(sorted(dropped))}. Make sure the join "
                                      "was really redundant.")
    return Check("sources", PASS, f"Reads the same table(s): {', '.join(sorted(after)) or 'none'}.")


def _schema_tables(schema: Mapping[str, Any] | None) -> set[str]:
    out: set[str] = set()

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for key, value in node.items():
            name = f"{prefix}{key}".lower()
            if isinstance(value, Mapping) and value and all(isinstance(v, Mapping) for v in value.values()):
                walk(value, f"{name}.")
            else:
                out.add(name)
                out.add(name.split(".")[-1])

    if schema:
        walk(schema, "")
    return out


def _outputs(statement: exp.Expr) -> list[str] | None:
    query = statement
    while isinstance(query, exp.Subquery):
        query = query.this
    if isinstance(query, exp.SetOperation):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    return [e.alias_or_name or e.sql() for e in query.expressions]


def _projection_check(old: list[exp.Expr], new: list[exp.Expr],
                      schema: Mapping[str, Any] | None = None) -> Check:
    for i, (o, n) in enumerate(zip(old, new, strict=False), 1):
        a, b = _outputs(o), _outputs(n)
        if a is None or b is None:
            continue
        if any(e == "*" for e in a) and not any(e == "*" for e in b):
            expected = _expanded_star_columns(o, schema)
            if expected is None:
                return Check("projection", FAIL,
                             f"Statement {i}: SELECT * was replaced by {len(b)} explicit column(s) "
                             f"({', '.join(b)}), but without a declared schema the expansion cannot be verified - "
                             "columns may be missing from the result.")
            if [c.lower() for c in expected] != [c.lower() for c in b]:
                return Check("projection", FAIL,
                             f"Statement {i}: SELECT * expands to {len(expected)} column(s) "
                             f"({', '.join(expected)}) but the rewrite projects {', '.join(b)}.")
            return Check("projection", PASS, f"SELECT * was expanded to exactly the {len(b)} schema column(s).")
        if len(a) != len(b):
            return Check("projection", FAIL,
                         f"Statement {i} projects {len(b)} column(s) instead of {len(a)}: "
                         f"{', '.join(a)} -> {', '.join(b)}.")
        if [x.lower() for x in a] != [x.lower() for x in b]:
            return Check("projection", WARN, f"Statement {i}: output column names changed "
                                             f"({', '.join(a)} -> {', '.join(b)}).")
    return Check("projection", PASS, "Same output columns in the same order.")


def _expanded_star_columns(statement: exp.Expr, schema: Mapping[str, Any] | None) -> list[str] | None:
    """Columns ``SELECT *`` stands for, when the declared schema covers every source table."""
    if not schema:
        return None
    query = statement
    while isinstance(query, exp.Subquery):
        query = query.this
    if not isinstance(query, exp.Select):
        return None
    from_ = query.args.get("from_") or query.args.get("from")
    sources = ([from_.this] if from_ is not None else []) + [j.this for j in query.args.get("joins") or []]
    columns: list[str] = []
    for source in sources:
        if not isinstance(source, exp.Table):
            return None
        cols = _schema_columns(schema, source)
        if cols is None:
            return None
        columns += cols
    return columns or None


def _schema_columns(schema: Mapping[str, Any], table: exp.Table) -> list[str] | None:
    parts = [p.lower() for p in (table.catalog, table.db, table.name) if p]
    node: Any = schema
    for part in parts:
        if isinstance(node, Mapping) and part in {k.lower() for k in node}:
            node = next(v for k, v in node.items() if k.lower() == part)
        elif isinstance(node, Mapping) and len(parts) == 1:
            node = None
            break
    if isinstance(node, Mapping) and node and all(not isinstance(v, Mapping) for v in node.values()):
        return list(node)
    # one level deeper: the schema may be nested (db -> table -> columns)
    if isinstance(schema, Mapping):
        for value in schema.values():
            if isinstance(value, Mapping) and table.name.lower() in {k.lower() for k in value}:
                inner = next(v for k, v in value.items() if k.lower() == table.name.lower())
                if isinstance(inner, Mapping):
                    return list(inner)
    return None


def _predicates(statement: exp.Expr, dialect: str) -> set[str]:
    out: set[str] = set()
    for node in statement.find_all(exp.Where, exp.Having, exp.Join, exp.Qualify):
        condition = node.args.get("on") if isinstance(node, exp.Join) else node.this
        if condition is None:
            continue
        for leaf in _leaves(condition):
            out.add(canonical_sql(leaf, dialect))
    return out


def _leaves(condition: exp.Expr) -> list[exp.Expr]:
    while isinstance(condition, exp.Paren):
        condition = condition.this
    if isinstance(condition, (exp.And, exp.Or)):
        return _leaves(condition.this) + _leaves(condition.expression)
    return [condition]


def _predicate_check(old: list[exp.Expr], new: list[exp.Expr], dialect: str) -> Check:
    before: set[str] = set()
    after: set[str] = set()
    for statement in old:
        before |= _predicates(statement, dialect)
    for statement in new:
        after |= _predicates(statement, dialect)
    dropped = sorted(before - after)
    added = sorted(after - before)
    if dropped and not added:
        return Check("predicates", FAIL, f"{len(dropped)} predicate(s) disappeared without replacement: "
                                         f"{'; '.join(dropped[:4])}.")
    if dropped:
        return Check("predicates", WARN, f"Predicates were rewritten ({len(dropped)} removed, {len(added)} added): "
                                         f"{'; '.join(dropped[:3])} -> {'; '.join(added[:3])}. Check that the "
                                         "conditions are equivalent.")
    if added:
        return Check("predicates", WARN, f"{len(added)} predicate(s) added: {'; '.join(added[:4])}.")
    return Check("predicates", PASS, f"All {len(before)} filter/join predicate(s) are still there.")


def _shape(statement: exp.Expr) -> dict[str, int]:
    return {
        "aggregates": len([a for a in statement.find_all(exp.AggFunc) if a.find_ancestor(exp.Window) is None]),
        "group keys": sum(len(g.expressions) for g in statement.find_all(exp.Group)),
        "DISTINCT": len(list(statement.find_all(exp.Distinct))),
        "LIMIT": len(list(statement.find_all(exp.Limit))),
        "ORDER BY": len([o for o in statement.find_all(exp.Order) if o.parent is statement]),
        "joins": len(list(statement.find_all(exp.Join))),
        "window functions": len(list(statement.find_all(exp.Window))),
    }


def _total_shape(statements: list[exp.Expr]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for statement in statements:
        for key, value in _shape(statement).items():
            totals[key] = totals.get(key, 0) + value
    return totals


def _shape_check(old: list[exp.Expr], new: list[exp.Expr],
                 rule_based: list[exp.Expr] | None = None) -> Check:
    before, after = _total_shape(old), _total_shape(new)
    # what the deterministic rules drop themselves is a known-safe reduction (e.g. a redundant DISTINCT)
    allowed = _total_shape(rule_based) if rule_based else {}
    lost, gained, excused = [], [], []
    for key, count in before.items():
        delta = f"{key} {count} -> {after.get(key, 0)}"
        if after.get(key, 0) < count:
            if allowed and allowed.get(key, count) <= after.get(key, 0):
                excused.append(delta)
            else:
                lost.append(delta)
        elif after.get(key, 0) > count:
            gained.append(delta)
    if lost:
        serious = [x for x in lost if x.split()[0] in ("aggregates", "group", "DISTINCT", "LIMIT", "ORDER")]
        return Check("query shape", FAIL if serious else WARN,
                     "The rewrite drops parts of the query: " + ", ".join(lost) + ".")
    if excused or gained:
        parts = []
        if excused:
            parts.append("the rule engine performs the same reduction (" + ", ".join(excused) + ")")
        if gained:
            parts.append("the rewrite adds " + ", ".join(gained))
        return Check("query shape", WARN, "Query shape changed: " + "; ".join(parts) + ".")
    return Check("query shape", PASS, "Aggregation, DISTINCT, ORDER BY, LIMIT and join count are unchanged.")


def _identity_check(old: list[exp.Expr], new: list[exp.Expr], dialect: str) -> Check:
    same = len(old) == len(new) and all(
        canonical_sql(o, dialect) == canonical_sql(n, dialect) for o, n in zip(old, new, strict=False))
    if same:
        return Check("changed anything", WARN, "The model returned the query unchanged (apart from formatting).")
    return Check("changed anything", PASS, "The rewrite differs from the original query.")


def _findings_check(old: list[exp.Expr], new: list[exp.Expr], hints: PartitionHints | None,
                    review: LlmReview) -> Check:
    before = analyze_static(old) + analyze_partitions(old, hints)
    after = analyze_static(new) + analyze_partitions(new, hints)
    old_keys = {f.key for f in before}
    new_keys = {f.key for f in after}
    review.introduced = [f for f in after if f.key not in old_keys]
    review.resolved = [f for f in before if f.key not in new_keys]
    errors = [f for f in review.introduced if f.severity is Severity.ERROR]
    if errors:
        return Check("static analysis", FAIL, "The rewrite introduces "
                     + ", ".join(sorted({f"{f.rule}: {f.message}" for f in errors})))
    if review.introduced:
        return Check("static analysis", WARN, f"{len(review.introduced)} new finding(s): "
                     + ", ".join(sorted({f.rule for f in review.introduced}))
                     + (f"; {len(review.resolved)} resolved." if review.resolved else "."))
    return Check("static analysis", PASS,
                 f"No new findings; {len(review.resolved)} resolved." if review.resolved
                 else "No new findings.")


def _rules_check(new: list[exp.Expr], dialect: str, schema: Mapping[str, Any] | None,
                 rules: OptimizationResult | None, review: LlmReview) -> Check:
    if rules is not None and rules.statements:
        review.matches_rules = len(rules.statements) == len(new) and all(
            canonical_sql(a, dialect) == canonical_sql(b, dialect)
            for a, b in zip(rules.statements, new, strict=False))
    after = optimize_query(new, dialect, schema=schema, include_review=False)
    review.missed_rules = sorted({rw.rule for rw in after.rewrites})
    if review.matches_rules:
        return Check("rule engine", PASS, "Identical to the deterministic rule-based rewrite.")
    if review.missed_rules:
        return Check("rule engine", WARN, "The rule engine can still improve the model's SQL: "
                     + ", ".join(review.missed_rules) + ".")
    safe_rules_before = rules and [rw.rule for rw in rules.rewrites if rw.safety is Safety.SAFE]
    if safe_rules_before:
        return Check("rule engine", PASS, "The model's SQL leaves no safe rule applicable; it covers the rule "
                                          f"engine's rewrites ({', '.join(sorted(set(safe_rules_before)))}).")
    return Check("rule engine", PASS, "No safe rewrite rule applies to the model's SQL.")
