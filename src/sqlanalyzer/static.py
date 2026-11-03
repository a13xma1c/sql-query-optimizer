"""Static (plan-free) lint, join and scan analysis built on SQLGlot ASTs."""

from __future__ import annotations

from collections import Counter

from sqlglot import exp
from sqlglot.optimizer.scope import ScopeType

from .models import Category, Finding, Severity
from .sqlutil import (
    comparison_sides,
    conjuncts,
    disjuncts,
    in_projection,
    is_constant,
    is_wrapped,
    locate,
    owners_in,
    real_tables,
    scopes_of,
    select_scopes,
    table_names,
)

MANY_JOINS = 6
_RANGE_OR_EQ = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.In, exp.Like, exp.ILike)


def _f(rule: str, sev: Severity, cat: Category, node: exp.Expression | None, msg: str, tip: str = "") -> Finding:
    line, col = locate(node) if node is not None else (None, None)
    return Finding(rule, sev, cat, msg, tip, line, col)


def analyze_static(statements: list[exp.Expression]) -> list[Finding]:
    findings: list[Finding] = []
    for stmt in statements:
        findings += lint(stmt)
        findings += analyze_joins(stmt)
        findings += analyze_scans(stmt)
    return findings


# --------------------------------------------------------------------------- lint
def lint(stmt: exp.Expression) -> list[Finding]:
    out: list[Finding] = []
    W, I = Severity.WARNING, Severity.INFO

    if isinstance(stmt, (exp.Delete, exp.Update)) and stmt.args.get("where") is None:
        kind = "DELETE" if isinstance(stmt, exp.Delete) else "UPDATE"
        out.append(_f("missing-where", W, Category.LINT, stmt.this,
                      f"{kind} without a WHERE clause affects every row.",
                      "Add a WHERE clause or confirm that a full-table change is intended."))

    for star in stmt.find_all(exp.Star):
        if isinstance(star.parent, (exp.Count, exp.Distinct)) or star.find_ancestor(exp.Exists):
            continue
        if isinstance(star.parent, exp.Column) and not star.parent.table:
            continue
        if isinstance(star.parent, (exp.Select, exp.Column)):
            out.append(_f("select-star", I, Category.LINT, star,
                          "SELECT * reads every column.",
                          "List only the columns you need; it reduces I/O (esp. on columnar stores) and breaks less on schema changes."))

    for eq in stmt.find_all(exp.EQ, exp.NEQ):
        if isinstance(eq.this, exp.Null) or isinstance(eq.expression, exp.Null):
            out.append(_f("null-comparison", W, Category.LINT, eq,
                          "Comparison with NULL using =/<> is never true.",
                          "Use IS NULL / IS NOT NULL."))

    for node in stmt.find_all(exp.Not):
        inner = node.this
        if isinstance(inner, exp.In) and inner.args.get("query") is not None:
            out.append(_f("not-in-subquery", W, Category.LINT, node,
                          "NOT IN (subquery) returns no rows if the subquery yields a NULL and is often slow.",
                          "Use NOT EXISTS or an anti-join (LEFT JOIN ... IS NULL)."))

    for union in stmt.find_all(exp.Union):
        if union.args.get("distinct"):
            out.append(_f("union-distinct", I, Category.LINT, union,
                          "UNION removes duplicates (requires sort/hash of the full result).",
                          "Use UNION ALL if duplicates are impossible or acceptable."))

    for order in stmt.find_all(exp.Order):
        for o in order.expressions:
            if isinstance(o.this, exp.Literal) and not o.this.is_string:
                out.append(_f("order-by-ordinal", I, Category.LINT, o.this,
                              "ORDER BY uses a column position.", "Reference column names/aliases for robustness."))
        parent = order.parent
        if isinstance(parent, exp.Select) and parent.args.get("limit") is None:
            if parent.parent is not None and isinstance(parent.parent, (exp.Subquery, exp.CTE)):
                out.append(_f("order-by-in-subquery", I, Category.LINT, order,
                              "ORDER BY in a subquery/CTE without LIMIT is usually wasted work.",
                              "Remove it or apply the ordering in the outermost query."))

    for sel in stmt.find_all(exp.Select):
        if sel.args.get("limit") is not None and sel.args.get("order") is None and sel.parent is None:
            out.append(_f("limit-without-order", I, Category.LINT, sel.args["limit"],
                          "LIMIT without ORDER BY returns an arbitrary set of rows.",
                          "Add ORDER BY for deterministic results."))
        having = sel.args.get("having")
        if having is not None:
            for c in conjuncts(having.this):
                if c.find(exp.AggFunc) is None and c.find(exp.Window) is None:
                    out.append(_f("having-without-aggregate", I, Category.LINT, c,
                                  "HAVING predicate has no aggregate.",
                                  "Move it to WHERE so rows are filtered before grouping."))

    for like in stmt.find_all(exp.Like, exp.ILike):
        pat = like.expression
        if isinstance(pat, exp.Literal) and pat.is_string and pat.this[:1] in ("%", "_"):
            out.append(_f("leading-wildcard", W, Category.SCAN, like,
                          "LIKE pattern starts with a wildcard; indexes and min/max pruning cannot be used.",
                          "Anchor the pattern, or use full-text / trigram / search-optimization features."))
    return out


# -------------------------------------------------------------------------- joins
def analyze_joins(stmt: exp.Expression) -> list[Finding]:
    out: list[Finding] = []
    W, I = Severity.WARNING, Severity.INFO

    for scope in select_scopes(stmt):
        sel = scope.expression
        joins: list[exp.Join] = sel.args.get("joins") or []
        tables = real_tables(scope)
        where = conjuncts(sel.args.get("where"))

        if len(joins) >= MANY_JOINS:
            out.append(_f("many-joins", W, Category.JOIN, joins[0],
                          f"Query block joins {len(joins) + 1} relations.",
                          "Join order search becomes unreliable; consider pre-aggregating, splitting into CTEs/temp tables, or denormalizing."))

        for join in joins:
            target = join.this
            if not isinstance(target, (exp.Table, exp.Subquery)):
                continue  # UNNEST, LATERAL, table functions
            alias = target.alias_or_name
            on = join.args.get("on")
            using = join.args.get("using")
            explicit_cross = (join.kind or "").upper() == "CROSS"
            comma = not (join.side or join.kind or on or using or join.args.get("method"))

            if explicit_cross or (comma and on is None and using is None):
                if _is_scalar_subquery(target):
                    continue
                linked = _linked_by_where(where, alias, tables)
                label = "CROSS JOIN" if explicit_cross else "Comma join"
                if linked:
                    out.append(_f("implicit-join", I, Category.JOIN, join,
                                  f"{label} to '{alias}' is joined only via WHERE.",
                                  "Use explicit JOIN ... ON syntax."))
                else:
                    out.append(_f("cartesian-join", Severity.ERROR, Category.JOIN, join,
                                  f"{label} to '{alias}' produces a Cartesian product (no join condition).",
                                  "Add a join predicate, or make sure one side is guaranteed to have a single row."))
                continue

            if on is not None:
                out += _check_on_clause(join, on, alias, tables)

            if (join.side or "").upper() == "LEFT":
                for c in where:
                    if isinstance(c, _RANGE_OR_EQ) and not isinstance(c, exp.Not):
                        cols = list(c.find_all(exp.Column))
                        if cols and all(col.table == alias for col in cols):
                            out.append(_f("left-join-filtered", I, Category.JOIN, c,
                                          f"WHERE filters '{alias}' after a LEFT JOIN, turning it into an INNER JOIN.",
                                          "Use INNER JOIN, or move the predicate into the ON clause to keep unmatched rows."))
                            break

    for scope in scopes_of(stmt):
        if (scope.scope_type == ScopeType.SUBQUERY and scope.is_correlated_subquery
                and in_projection(scope.expression)):
            out.append(_f("correlated-subquery", W, Category.JOIN, scope.expression,
                          "Correlated scalar subquery in SELECT list is evaluated per outer row.",
                          "Rewrite as a JOIN against a pre-aggregated subquery or use a window function."))
    return out


def _is_scalar_subquery(target: exp.Expression) -> bool:
    if not isinstance(target, exp.Subquery):
        return False
    inner = target.this
    return (isinstance(inner, exp.Select) and inner.args.get("group") is None
            and inner.find(exp.AggFunc) is not None) or (
        isinstance(inner, exp.Select) and inner.args.get("limit") is not None
        and getattr(inner.args["limit"].expression, "this", None) == "1")


def _linked_by_where(where: list[exp.Expression], alias: str, tables: dict[str, exp.Table]) -> bool:
    for c in where:
        if isinstance(c, exp.EQ):
            left, right = owners_in(c.this, tables), owners_in(c.expression, tables)
            if left and right and left != right and alias in (left | right):
                return True
    return False


def _check_on_clause(join: exp.Join, on: exp.Expression, alias: str, tables: dict[str, exp.Table]) -> list[Finding]:
    out: list[Finding] = []
    W = Severity.WARNING
    parts = conjuncts(on)
    equi = []
    for p in parts:
        if isinstance(p, exp.EQ):
            left, right = owners_in(p.this, tables), owners_in(p.expression, tables)
            cross_side = (left and right and left != right) or (
                p.this.find(exp.Column) and p.expression.find(exp.Column)
                 and _columns_differ(p))
            if cross_side:
                equi.append(p)

    if not equi:
        out.append(_f("non-equi-join", W, Category.JOIN, on,
                      f"JOIN to '{alias}' has no equality condition between the two sides.",
                      "Hash/merge joins need an equi-condition; otherwise the engine falls back to a nested-loop join (O(n*m))."))
    else:
        for p in equi:
            for col in p.find_all(exp.Column):
                if is_wrapped(col, p):
                    out.append(_f("join-on-expression", W, Category.JOIN, p,
                                  f"Join key '{col.sql()}' is wrapped in a function/cast/arithmetic expression.",
                                  "Wrapping join keys blocks index use and can force a slower join strategy; align the data types or precompute the key."))
                    break
    for p in parts:
        if len(disjuncts(p)) > 1:
            out.append(_f("or-in-join", W, Category.JOIN, p,
                          "OR in a JOIN condition usually prevents hash joins.",
                          "Split into UNION ALL of two joins, or join on a derived key."))
    return out


def _columns_differ(eq: exp.EQ) -> bool:
    lt = {c.table for c in eq.this.find_all(exp.Column) if c.table}
    rt = {c.table for c in eq.expression.find_all(exp.Column) if c.table}
    return bool(lt and rt and lt != rt)


# -------------------------------------------------------------------------- scans
def analyze_scans(stmt: exp.Expression) -> list[Finding]:
    out: list[Finding] = []
    W, I = Severity.WARNING, Severity.INFO
    occurrences: Counter[str] = Counter()
    first_node: dict[str, exp.Table] = {}

    for scope in select_scopes(stmt):
        sel = scope.expression
        tables = real_tables(scope)
        preds = conjuncts(sel.args.get("where"))
        for j in sel.args.get("joins") or []:
            if j.args.get("on") is not None:
                preds += conjuncts(j.args["on"])

        for p in preds:
            sides = comparison_sides(p)
            if not any(is_constant(s) for s in sides):
                continue
            for s in sides:
                if is_constant(s):
                    continue
                for col in s.find_all(exp.Column):
                    if is_wrapped(col, p) and not isinstance(p, (exp.Like, exp.ILike)):
                        out.append(_f("non-sargable-predicate", W, Category.SCAN, p,
                                      f"Column '{col.sql()}' is wrapped in an expression inside a predicate.",
                                      "Compare the bare column to a transformed constant (e.g. ts >= '2024-01-01' AND ts < '2024-01-02') so indexes/pruning apply."))
                        break

        for table in tables.values():
            _, qualified = table_names(table)
            occurrences[qualified] += 1
            first_node.setdefault(qualified, table)

        if (tables and sel.args.get("where") is None and sel.args.get("limit") is None
                and not scope.is_correlated_subquery and not _only_counted(sel)):
            names = ", ".join(sorted({table_names(t)[1] for t in tables.values()}))
            out.append(_f("unfiltered-scan", I, Category.SCAN, next(iter(tables.values())),
                          f"No WHERE clause: {names} will be read in full.",
                          "If the data is large, add a selective predicate (ideally on a partition/clustering column)."))

        for where in conjuncts(sel.args.get("where")):
            if len(disjuncts(where)) > 1:
                cols = {c.sql() for d in disjuncts(where) for c in d.find_all(exp.Column)}
                if len(cols) > 1:
                    out.append(_f("or-across-columns", I, Category.SCAN, where,
                                  "OR over different columns often prevents index use and pruning.",
                                  "Consider UNION ALL of separately-filtered queries."))

    for name, n in occurrences.items():
        if n >= 2:
            out.append(_f("repeated-scan", W if n >= 3 else I, Category.SCAN, first_node[name],
                          f"Table '{name}' is scanned {n} times in this statement.",
                          "Consider a CTE/temp table, conditional aggregation or window functions to scan it once."))
    return out


def _only_counted(sel: exp.Select) -> bool:
    """SELECT COUNT(*) FROM t is a metadata/fast-path scan on most engines."""
    exprs = sel.expressions
    return len(exprs) == 1 and isinstance(exprs[0].unalias(), exp.Count) and sel.args.get("where") is None
