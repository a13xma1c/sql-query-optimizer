"""Rule-based query optimization engine.

The engine takes a SQLGlot AST and repeatedly applies :class:`Rule` objects
until no rule changes the tree (or a pass limit is hit). Every change is
recorded as a :class:`Rewrite` with a before/after SQL fragment, so the user
can see *what* was rewritten and *why*.

Design notes
------------
* Rules work on a **copy** of the tree. After a rule reports rewrites, the
  engine validates the result (it must round-trip through the dialect parser
  and must not reference unknown table aliases). Invalid results are discarded
  and reported in :attr:`OptimizationResult.errors`, so a buggy or overly
  eager rule can never corrupt the query.
* Rules are marked :attr:`Safety.SAFE` (semantics preserving) or
  :attr:`Safety.REVIEW` (usually desirable but may change results, e.g.
  ``UNION`` -> ``UNION ALL``). Review rules can be switched off.
* Some rules need every column to be resolved to its source table (SQLGlot's
  *qualified* form). Those are only run when :func:`qualify` succeeds, which
  requires either a schema or fully qualified, star-free SQL.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import traverse_scope

from ..sqlutil import locate

log = logging.getLogger(__name__)

MAX_PASSES = 6
QUALIFY_RULE = "qualify"


class Safety(StrEnum):
    SAFE = "safe"  # semantics preserving
    REVIEW = "review"  # usually desirable, but results may differ in edge cases


@dataclass(frozen=True)
class Rewrite:
    """One applied transformation."""

    rule: str
    title: str
    category: str
    safety: Safety
    description: str
    before: str
    after: str
    fixes: tuple[str, ...] = ()  # static-analysis finding rules this rewrite addresses
    line: int | None = None
    col: int | None = None


@dataclass
class RuleContext:
    dialect: str
    schema: Mapping[str, Any] | None = None
    qualified: bool = False  # columns resolved to their tables, no SELECT *
    include_review: bool = True  # rules may emit Safety.REVIEW rewrites
    extra: dict[str, Any] = field(default_factory=dict)


class Rule(ABC):
    """A rewrite rule. Subclasses implement :meth:`apply`."""

    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str] = "general"
    safety: ClassVar[Safety] = Safety.SAFE
    requires_qualified: ClassVar[bool] = False
    fixes: ClassVar[tuple[str, ...]] = ()

    @abstractmethod
    def apply(self, expression: exp.Expression, ctx: RuleContext) -> tuple[exp.Expression, list[Rewrite]]:
        """Rewrite ``expression`` in place (or return a new root) and describe every change."""

    def precheck(self, expression: exp.Expression) -> bool:
        """Cheap test whether the rule can possibly apply; saves copying the tree when it cannot."""
        return True

    # ------------------------------------------------------------------ helpers
    def rewrite(self, before: exp.Expression | str, after: exp.Expression | str, description: str,
                ctx: RuleContext | None = None, node: exp.Expression | None = None,
                safety: Safety | None = None) -> Rewrite:
        """Build a :class:`Rewrite`; ``node`` (default: ``before``) supplies the source position."""
        dialect = ctx.dialect if ctx else None
        if isinstance(before, exp.Expression):
            node = node or before
            before = before.sql(dialect=dialect)
        if isinstance(after, exp.Expression):
            after = after.sql(dialect=dialect)
        line, col = locate(node) if node is not None else (None, None)
        return Rewrite(self.name, self.title, self.category, safety or self.safety, description, before, after,
                       self.fixes, line, col)

    def __repr__(self) -> str:
        return f"<Rule {self.name}>"


@dataclass
class OptimizationResult:
    dialect: str
    original_sql: str
    optimized_sql: str
    statements: list[exp.Expression]
    rewrites: list[Rewrite] = field(default_factory=list)
    passes: int = 0
    qualified: bool = False
    skipped: dict[str, str] = field(default_factory=dict)  # rule -> reason it did not run
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.rewrites)

    @property
    def safe_only(self) -> bool:
        return all(r.safety is Safety.SAFE for r in self.rewrites)

    def by_rule(self) -> dict[str, list[Rewrite]]:
        out: dict[str, list[Rewrite]] = {}
        for r in self.rewrites:
            out.setdefault(r.rule, []).append(r)
        return out

    def summary(self) -> str:
        if not self.rewrites:
            return "No rewrites applied."
        n_review = sum(r.safety is Safety.REVIEW for r in self.rewrites)
        parts = [f"{len(self.rewrites)} rewrite{'s' if len(self.rewrites) != 1 else ''} in {self.passes} pass"
                 + ("es" if self.passes != 1 else "")]
        if n_review:
            parts.append(f"{n_review} need review")
        return ", ".join(parts) + "."


# ----------------------------------------------------------------------- validation
_COMMUTATIVE = (exp.EQ, exp.NEQ, exp.NullSafeEQ, exp.NullSafeNEQ, exp.Add, exp.Mul)


def canonical_sql(expression: exp.Expression, dialect: str | None = None) -> str:
    """SQL with commutative operands and AND/OR chains in a fixed order.

    Used to decide whether a rule *really* changed a query: SQLGlot's passes like
    to swap ``a = b`` into ``b = a`` or reorder conjuncts, which is not a rewrite
    worth reporting.
    """
    root = expression.copy()

    def canon(node: exp.Expression) -> exp.Expression:
        for child in list(node.iter_expressions()):
            new_child = canon(child)
            if new_child is not child:
                child.replace(new_child)
        if isinstance(node, _COMMUTATIVE) and node.expression is not None:
            if node.this.sql() > node.expression.sql():
                return type(node)(this=node.expression, expression=node.this)
        if isinstance(node, (exp.And, exp.Or)) and not isinstance(node.parent, type(node)):
            parts = sorted(node.flatten(), key=lambda p: p.sql())
            func = exp.and_ if isinstance(node, exp.And) else exp.or_
            return func(*[p.copy() for p in parts], copy=False)
        if isinstance(node, exp.Join) and node.kind == "INNER" and not node.side:
            node.set("kind", None)  # JOIN and INNER JOIN are the same thing
        return node

    return canon(root).sql(dialect=dialect)


def invalid_references(expression: exp.Expression) -> list[str]:
    """Qualified columns whose table alias is not visible in their scope (or any outer scope)."""
    bad: list[str] = []
    try:
        scopes = traverse_scope(expression)
    except (SqlglotError, KeyError, AttributeError):  # pragma: no cover - defensive
        return bad
    for scope in scopes:
        for col in scope.columns:
            if not col.table:
                continue
            s = scope
            while s is not None and col.table not in s.sources:
                s = s.parent
            if s is None:
                bad.append(col.sql())
    return bad


def validate(expression: exp.Expression, dialect: str) -> str | None:
    """Return an error message if ``expression`` is not a sane query, else ``None``."""
    try:
        sql = expression.sql(dialect=dialect)
        reparsed = sqlglot.parse_one(sql, read=dialect)
    except SqlglotError as exc:
        return f"result does not round-trip through the {dialect} parser: {exc}"
    if reparsed is None:  # pragma: no cover - defensive
        return "result is empty"
    bad = invalid_references(expression)
    if bad:
        return "result references unknown tables: " + ", ".join(sorted(set(bad)))
    return None


def try_qualify(expression: exp.Expression, dialect: str, schema: Mapping[str, Any] | None) -> exp.Expression | None:
    """Resolve every column to its table (expanding ``*`` when a schema is known). ``None`` on failure."""
    try:
        q = qualify(expression.copy(), dialect=dialect, schema=schema, quote_identifiers=False)
    except (SqlglotError, KeyError, AttributeError, TypeError, ValueError):
        return None
    # SQLGlot's scope based passes need star-free selections (else they cannot map columns).
    for star in q.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Select) or (isinstance(parent, exp.Column) and isinstance(parent.parent, exp.Select)):
            return None
    return q


def strip_qualification_noise(expression: exp.Expression) -> exp.Expression:
    """Undo purely cosmetic artefacts of :func:`qualify`.

    ``o.id AS id`` -> ``o.id``, ``orders AS orders`` -> ``orders`` and, in the
    outermost select only, ``1 AS "1"`` -> ``1``. None of these change the
    result or the names the query exposes.
    """
    for alias in list(expression.find_all(exp.Alias)):
        sel = alias.parent
        if not isinstance(sel, exp.Select) or alias.args.get("alias") is None:
            continue
        inner, name = alias.this, alias.alias
        if isinstance(inner, exp.Column) and inner.name == name and not alias.args["alias"].quoted:
            alias.replace(inner)
        elif _outputs_unreferenced(sel) and (isinstance(inner, exp.Literal) and inner.this == name
                                             or re.fullmatch(r"_col_\d+", name)):
            alias.replace(inner)  # generated names on the outermost select would change the result headers
    for sel in expression.find_all(exp.Select):
        if _outputs_unreferenced(sel):
            for sub in sel.expressions:
                if isinstance(sub, exp.Subquery) and re.fullmatch(r"_col_\d+", sub.alias or ""):
                    sub.set("alias", None)
    for table in expression.find_all(exp.Table):
        table_alias = table.args.get("alias")
        if isinstance(table_alias, exp.TableAlias) and table_alias.name == table.name and not table_alias.args.get("columns") \
                and not table_alias.this.quoted:
            table.set("alias", None)
    return expression


def _outputs_unreferenced(sel: exp.Select) -> bool:
    """True for the statement root and for scalar / IN / EXISTS subqueries (nobody names their columns)."""
    parent = sel.parent
    if parent is None or isinstance(parent, (exp.In, exp.Exists)):
        return True
    return isinstance(parent, exp.Subquery) and not isinstance(parent.parent, (exp.From, exp.Join, exp.With, exp.CTE))


# --------------------------------------------------------------------------- engine
class Optimizer:
    def __init__(self, rules: Sequence[Rule], *, include_review: bool = True, max_passes: int = MAX_PASSES,
                 disabled: Iterable[str] = ()) -> None:
        self.rules = list(rules)
        self.include_review = include_review
        self.max_passes = max_passes
        self.disabled = set(disabled)

    def optimize(self, statements: Sequence[exp.Expression], dialect: str,
                 schema: Mapping[str, Any] | None = None) -> OptimizationResult:
        original = _render(statements, dialect)
        result = OptimizationResult(dialect, original, original, [])
        for stmt in statements:
            optimized = self._optimize_statement(stmt, dialect, schema, result)
            result.statements.append(optimized)
        result.optimized_sql = _render(result.statements, dialect) if result.changed else original
        return result

    def _optimize_statement(self, stmt: exp.Expression, dialect: str, schema: Mapping[str, Any] | None,
                            result: OptimizationResult) -> exp.Expression:
        ctx = RuleContext(dialect, schema, include_review=self.include_review)
        current = stmt.copy()
        rules = self._active_rules(result)

        # Name resolution runs once, first: later rules rely on ctx.qualified.
        qualifier = next((r for r in rules if r.name == QUALIFY_RULE), None)
        if qualifier is not None:
            rules = [r for r in rules if r is not qualifier]
            candidate, rewrites = self._try_rule(qualifier, current, ctx, result)
            if candidate is not None:
                current = candidate
                result.rewrites += rewrites
            result.qualified = result.qualified or ctx.qualified

        current_key = canonical_sql(current, dialect)
        seen = {current_key}
        for n in range(1, self.max_passes + 1):
            result.passes = max(result.passes, n)
            changed = False
            for rule in rules:
                if rule.requires_qualified and not ctx.qualified:
                    result.skipped.setdefault(rule.name, "needs every column resolved to its table (declare the "
                                                         "schema, or qualify all columns and avoid SELECT *)")
                    continue
                candidate, rewrites = self._try_rule(rule, current, ctx, result)
                if candidate is None or not rewrites:
                    continue
                sql = canonical_sql(candidate, dialect)
                if sql == current_key:
                    continue  # only cosmetic differences (operand order): ignore
                if sql in seen:
                    result.errors.append(f"{rule.name}: rewrite discarded, it undoes an earlier rewrite")
                    continue
                seen.add(sql)
                current, current_key = candidate, sql
                result.rewrites += rewrites
                changed = True
            if not changed:
                break
        if ctx.qualified:
            strip_qualification_noise(current)
        return current

    def _try_rule(self, rule: Rule, current: exp.Expression, ctx: RuleContext,
                  result: OptimizationResult) -> tuple[exp.Expression | None, list[Rewrite]]:
        """Run ``rule`` on a copy; return (new tree, rewrites) or (None, []) if nothing usable came out."""
        was_qualified = ctx.qualified
        try:
            if not rule.precheck(current):
                return None, []
            candidate, rewrites = rule.apply(current.copy(), ctx)
        except Exception as exc:  # noqa: BLE001 - a broken rule must not abort the run
            log.debug("rule %s failed", rule.name, exc_info=True)
            result.errors.append(f"{rule.name}: {exc.__class__.__name__}: {exc}")
            ctx.qualified = was_qualified
            return None, []
        if not rewrites and not (rule.name == QUALIFY_RULE and ctx.qualified and not was_qualified):
            return None, []  # nothing happened: no need to validate
        if not self.include_review and any(r.safety is Safety.REVIEW for r in rewrites):
            result.errors.append(f"{rule.name}: rewrite discarded, it needs review and review rules are off")
            ctx.qualified = was_qualified
            return None, []
        problem = validate(candidate, ctx.dialect)
        if problem:
            result.errors.append(f"{rule.name}: rewrite discarded, {problem}")
            ctx.qualified = was_qualified
            return None, []
        return candidate, rewrites

    def _active_rules(self, result: OptimizationResult) -> list[Rule]:
        active = []
        for r in self.rules:
            if r.name in self.disabled:
                result.skipped.setdefault(r.name, "disabled")
            elif r.safety is Safety.REVIEW and not self.include_review:
                result.skipped.setdefault(r.name, "needs review; review rules are switched off")
            else:
                active.append(r)
        return active


def _render(statements: Sequence[exp.Expression], dialect: str) -> str:
    pieces = [s.sql(dialect=dialect, pretty=True) for s in statements]
    return ";\n\n".join(pieces) + (";" if pieces else "")
