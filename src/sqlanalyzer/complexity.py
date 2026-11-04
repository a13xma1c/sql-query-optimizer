"""Query complexity metrics and a 0-100 score."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlglot import exp
from sqlglot.optimizer.scope import ScopeType

from .models import Category, Finding, Severity
from .sqlutil import conjuncts, scopes_of


@dataclass
class ComplexityReport:
    metrics: dict[str, int] = field(default_factory=dict)
    score: int = 0
    level: str = "Low"
    contributions: dict[str, float] = field(default_factory=dict)

    def findings(self) -> list[Finding]:
        if self.level in ("High", "Very high"):
            top = sorted(self.contributions.items(), key=lambda kv: -kv[1])[:3]
            drivers = ", ".join(f"{k} ({self.metrics[k]})" for k, _ in top if k in self.metrics)
            return [Finding("high-complexity", Severity.WARNING, Category.COMPLEXITY,
                            f"Query complexity is {self.level.lower()} (score {self.score}/100); main drivers: {drivers}.",
                            "Break the query into smaller CTEs/views, or simplify joins and nesting.")]
        return []


# metric -> (weight per unit, cap on contribution)
_WEIGHTS: dict[str, tuple[float, float]] = {
    "joins": (4.0, 28),
    "subqueries": (4.0, 16),
    "max_nesting_depth": (4.0, 16),
    "ctes": (1.5, 8),
    "set_operations": (3.0, 9),
    "window_functions": (2.5, 10),
    "aggregations": (1.0, 6),
    "predicates": (0.6, 8),
    "case_expressions": (1.0, 5),
    "distinct_tables": (1.0, 8),
    "correlated_subqueries": (5.0, 10),
}


def analyze_complexity(statements: list[exp.Expr]) -> ComplexityReport:
    m = dict.fromkeys(_WEIGHTS, 0)
    m["statements"] = len(statements)
    tables: set[str] = set()
    for stmt in statements:
        m["joins"] += len(list(stmt.find_all(exp.Join)))
        m["ctes"] += len(list(stmt.find_all(exp.CTE)))
        m["set_operations"] += len(list(stmt.find_all(exp.SetOperation)))
        m["window_functions"] += len(list(stmt.find_all(exp.Window)))
        m["aggregations"] += len(list(stmt.find_all(exp.AggFunc)))
        m["case_expressions"] += len(list(stmt.find_all(exp.Case)))
        m["subqueries"] += sum(
            1 for s in stmt.find_all(exp.Subquery)
            if not isinstance(s.parent, (exp.From, exp.Join, exp.CTE)) or s.find_ancestor(exp.Where)
        ) + len(list(stmt.find_all(exp.Exists)))
        for cond in (*stmt.find_all(exp.Where), *stmt.find_all(exp.Having)):
            m["predicates"] += len(conjuncts(cond.this))
        for j in stmt.find_all(exp.Join):
            if j.args.get("on") is not None:
                m["predicates"] += len(conjuncts(j.args["on"]))
        for t in stmt.find_all(exp.Table):
            if isinstance(t.this, exp.Identifier):
                tables.add(".".join(p for p in (t.catalog, t.db, t.name) if p).lower())
        m["max_nesting_depth"] = max(m["max_nesting_depth"], _depth(stmt))
        for scope in scopes_of(stmt):
            if scope.scope_type == ScopeType.SUBQUERY and scope.is_correlated_subquery:
                m["correlated_subqueries"] += 1
    # CTE names look like tables; drop them
    cte_names = {c.alias.lower() for stmt in statements for c in stmt.find_all(exp.CTE)}
    m["distinct_tables"] = len({t for t in tables if t not in cte_names})

    contrib = {k: min(v * _WEIGHTS[k][0], _WEIGHTS[k][1]) for k, v in m.items() if k in _WEIGHTS}
    score = min(100, round(sum(contrib.values())))
    level = ("Low" if score < 25 else "Moderate" if score < 50 else "High" if score < 75 else "Very high")
    return ComplexityReport(m, score, level, contrib)


def _depth(node: exp.Expr) -> int:
    """Maximum nesting of SELECT blocks (a plain statement is depth 1)."""
    best = 0

    def walk(n: exp.Expr, d: int) -> None:
        nonlocal best
        if isinstance(n, exp.Select):
            d += 1
            best = max(best, d)
        for child in n.iter_expressions():
            walk(child, d)

    walk(node, 0)
    return best
