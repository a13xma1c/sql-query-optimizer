"""Engine-specific static rules.

The generic rules in :mod:`sqlanalyzer.static` hold for every engine. The rules
here encode what is *specific* to PostgreSQL, DuckDB, Trino and Snowflake -
what each one materialises, shuffles, prunes or cannot index - and only fire for
the engines listed on each rule. Findings use the ``engine`` category.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlglot import exp

from .dialects import Engine
from .models import Category, Finding, Severity
from .optimizer.schema import TableStats
from .sqlutil import conjuncts, locate, select_scopes, table_names

COLUMNAR = frozenset({Engine.DUCKDB, Engine.TRINO, Engine.SNOWFLAKE})
DISTRIBUTED = frozenset({Engine.TRINO, Engine.SNOWFLAKE})
LARGE_OFFSET = 1000
LONG_IN_LIST = {Engine.POSTGRES: 100, Engine.DUCKDB: 500, Engine.TRINO: 1000, Engine.SNOWFLAKE: 1000}


@dataclass(frozen=True)
class EngineRule:
    name: str
    engines: frozenset[Engine]
    description: str
    check: Callable[[exp.Expr, Engine, Mapping[str, TableStats]], list[Finding]]


def _f(rule: str, sev: Severity, node: exp.Expr | None, msg: str, tip: str) -> Finding:
    line, col = locate(node) if node is not None else (None, None)
    return Finding(rule, sev, Category.ENGINE, msg, tip, line, col)


# ------------------------------------------------------------------------ rules
def _count_distinct(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for count in stmt.find_all(exp.Count):
        if isinstance(count.this, exp.Distinct):
            fn = "APPROX_DISTINCT" if engine is Engine.TRINO else "APPROX_COUNT_DISTINCT"
            out.append(_f("count-distinct-exact", Severity.INFO, count,
                          f"COUNT(DISTINCT ...) needs an exact, shuffled de-duplication on {engine.value}.",
                          f"If a ~2% error is acceptable use {fn}(...) (HyperLogLog), which streams in constant "
                          "memory."))
    return out


def _global_sort(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for sel in stmt.find_all(exp.Select):
        order = sel.args.get("order")
        if order is None or sel.args.get("limit") is not None or sel.parent is not None:
            continue
        if sel.args.get("joins") or sel.args.get("group") or sel.find(exp.Window):
            out.append(_f("global-sort-without-limit", Severity.INFO, order,
                          "Final ORDER BY without LIMIT: the distributed engine gathers the whole result on one "
                          "node to sort it.",
                          "Add a LIMIT (top-N runs in parallel), sort in the client, or drop the ORDER BY when the "
                          "result feeds another query."))
    return out


def _large_offset(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for offset in stmt.find_all(exp.Offset):
        value = offset.expression
        if isinstance(value, exp.Literal) and not value.is_string and float(value.this) >= LARGE_OFFSET:
            sev = Severity.WARNING if engine in (Engine.POSTGRES, Engine.DUCKDB) else Severity.INFO
            out.append(_f("large-offset", sev, offset,
                          f"OFFSET {value.this}: the engine still produces and discards the skipped rows.",
                          "Paginate with a keyset (WHERE sort_key > :last_seen ORDER BY sort_key LIMIT n) instead "
                          "of OFFSET."))
    return out


def _long_in_list(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    threshold = LONG_IN_LIST[engine]
    for in_ in stmt.find_all(exp.In):
        if in_.args.get("query") is None and len(in_.expressions) >= threshold:
            if engine is Engine.POSTGRES:
                tip = ("Use col = ANY(ARRAY[...]) (one parameter, index friendly) or join against a VALUES list / "
                       "temporary table.")
            else:
                tip = "Join against a VALUES list or a temporary table; long IN lists are planned as giant OR chains."
            out.append(_f("long-in-list", Severity.INFO, in_,
                          f"IN list with {len(in_.expressions)} literals.", tip))
    return out


def _case_insensitive_like(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for like in stmt.find_all(exp.Like):
        if isinstance(like.this, (exp.Lower, exp.Upper)) and like.this.find(exp.Column) is not None:
            if engine is Engine.POSTGRES:
                tip = ("Use ILIKE with a pg_trgm GIN index (col gin_trgm_ops), or an expression index on lower(col) "
                       "for anchored patterns.")
            elif engine is Engine.SNOWFLAKE:
                tip = "Use ILIKE; for exact case-insensitive matches use a COLLATE 'en-ci' column."
            else:
                tip = "Use ILIKE so the comparison stays on the bare column."
            out.append(_f("case-insensitive-like", Severity.INFO, like,
                          "LOWER()/UPPER() around the column makes the LIKE non-sargable.", tip))
    return out


def _cte_materialization(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out: list[Finding] = []
    with_ = stmt.args.get("with_") or stmt.args.get("with")
    if not isinstance(with_, exp.With):
        return out
    for cte in with_.expressions:
        refs = [t for t in stmt.find_all(exp.Table) if t.name.lower() == cte.alias.lower() and not t.db
                and t.find_ancestor(exp.CTE) is not cte]
        if len(refs) >= 2:
            out.append(_f("cte-materialized", Severity.INFO, cte,
                          f"CTE '{cte.alias}' is referenced {len(refs)} times: PostgreSQL materialises it, so outer "
                          "predicates are not pushed into it.",
                          "Filter inside the CTE, or write WITH ... AS NOT MATERIALIZED (PostgreSQL 12+) when "
                          "inlining is cheaper."))
    return out


def _variant_filter(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for where in stmt.find_all(exp.Where):
        for pred in conjuncts(where.this):
            extract = pred.find(exp.JSONExtract, exp.JSONExtractScalar)
            if extract is not None and extract.find(exp.Column) is not None:
                out.append(_f("variant-field-filter", Severity.WARNING, pred,
                              "Filtering on a VARIANT/JSON path: Snowflake cannot prune micro-partitions on it and "
                              "parses the document for every row.",
                              "Materialise the field as a real column (or a computed column with a clustering "
                              "key) and filter on that."))
    return out


def _join_build_side(stmt: exp.Expr, engine: Engine, stats: Mapping[str, TableStats]) -> list[Finding]:
    """Trino builds the hash table from the right side: warn when that side is known to be the bigger one."""
    out: list[Finding] = []
    if not stats:
        return out
    for scope in select_scopes(stmt):
        sel = scope.expression
        from_ = sel.args.get("from_") or sel.args.get("from")
        if from_ is None or not isinstance(from_.this, exp.Table):
            continue
        left_rows = _rows(from_.this, stats)
        for join in sel.args.get("joins") or []:
            if not isinstance(join.this, exp.Table) or (join.side or "").upper() in ("RIGHT", "FULL"):
                continue
            right_rows = _rows(join.this, stats)
            if left_rows and right_rows and right_rows >= 4 * left_rows:
                out.append(_f("join-build-side", Severity.INFO, join,
                              f"'{join.this.name}' (~{right_rows:,.0f} rows) is on the build (right) side of the "
                              f"join; '{from_.this.name}' has ~{left_rows:,.0f}.",
                              "Trino hashes the right side: put the smaller relation on the right, or rely on "
                              "cost-based join reordering with up-to-date table statistics (ANALYZE)."))
            left_rows = max(left_rows or 0, right_rows or 0) or None
    return out


def _unanchored_regex(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for node in stmt.find_all(exp.RegexpLike, exp.SimilarTo):
        pattern = node.expression
        if isinstance(pattern, exp.Literal) and pattern.is_string and not pattern.this.startswith("^") \
                and node.find_ancestor(exp.Where) is not None:
            out.append(_f("unanchored-regex", Severity.INFO, node,
                          "Regular-expression filter without a leading anchor scans every value completely.",
                          "Anchor the pattern (^...), or pre-filter with a cheaper predicate (LIKE 'abc%', "
                          "equality on a derived column)."))
    return out


def _columnar_select_star(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for star in stmt.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Column):
            parent = parent.parent
        if isinstance(parent, exp.Select) and parent.parent is None and parent.args.get("from_"):
            out.append(_f("columnar-select-star", Severity.WARNING, star,
                          f"SELECT * on a columnar engine ({engine.value}) reads every column from storage.",
                          "Project only the needed columns; column pruning is the biggest I/O saving on columnar "
                          "formats."))
    return out


def _postgres_distinct_on_order(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    out = []
    for sel in stmt.find_all(exp.Select):
        distinct = sel.args.get("distinct")
        if distinct is not None and distinct.args.get("on") and sel.args.get("order") is None:
            out.append(_f("distinct-on-without-order", Severity.WARNING, distinct,
                          "DISTINCT ON without ORDER BY keeps an arbitrary row per group.",
                          "Add ORDER BY <distinct columns>, <tie-breaker> to make the chosen row deterministic."))
    return out


def _duckdb_string_date_compare(stmt: exp.Expr, engine: Engine, _stats) -> list[Finding]:
    """DuckDB compares timestamp columns to string literals by casting the column when types mismatch."""
    out = []
    for cmp in stmt.find_all(exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
        left, right = cmp.this, cmp.expression
        if isinstance(left, exp.Cast) and isinstance(left.this, exp.Column) and left.to.is_type("VARCHAR", "TEXT") \
                and isinstance(right, exp.Literal):
            out.append(_f("cast-column-to-string", Severity.WARNING, cmp,
                          "The column is cast to a string before comparison; zone maps and indexes cannot be used.",
                          "Compare against a typed literal instead (DATE '...', TIMESTAMP '...', or a number)."))
    return out


def _rows(table: exp.Table, stats: Mapping[str, TableStats]) -> float | None:
    bare, qualified = table_names(table)
    for key in (qualified, bare):
        rows = stats[key].rows if key in stats else None
        if rows:
            return float(rows)
    for name, value in stats.items():
        if name.split(".")[-1] == bare and value.rows:
            return float(value.rows)
    return None


ENGINE_RULES: list[EngineRule] = [
    EngineRule("count-distinct-exact", DISTRIBUTED,
               "COUNT(DISTINCT) forces an exact shuffled de-duplication; APPROX_* is constant-memory.", _count_distinct),
    EngineRule("global-sort-without-limit", DISTRIBUTED,
               "A final ORDER BY without LIMIT is executed on a single node.", _global_sort),
    EngineRule("large-offset", frozenset(Engine),
               "OFFSET pagination produces and discards the skipped rows.", _large_offset),
    EngineRule("long-in-list", frozenset(Engine),
               "Very long IN lists are planned as OR chains.", _long_in_list),
    EngineRule("case-insensitive-like", frozenset({Engine.POSTGRES, Engine.DUCKDB, Engine.SNOWFLAKE}),
               "LOWER(col) LIKE defeats indexes; ILIKE / trigram indexes do not.", _case_insensitive_like),
    EngineRule("cte-materialized", frozenset({Engine.POSTGRES}),
               "PostgreSQL materialises CTEs that are referenced more than once.", _cte_materialization),
    EngineRule("variant-field-filter", frozenset({Engine.SNOWFLAKE}),
               "VARIANT path filters are not pruned.", _variant_filter),
    EngineRule("join-build-side", frozenset({Engine.TRINO}),
               "Trino builds the hash table from the right side of a join.", _join_build_side),
    EngineRule("unanchored-regex", frozenset(Engine),
               "Regular expressions without anchors scan whole values.", _unanchored_regex),
    EngineRule("columnar-select-star", COLUMNAR,
               "SELECT * reads every column from columnar storage.", _columnar_select_star),
    EngineRule("distinct-on-without-order", frozenset({Engine.POSTGRES}),
               "DISTINCT ON needs ORDER BY to be deterministic.", _postgres_distinct_on_order),
    EngineRule("cast-column-to-string", frozenset({Engine.DUCKDB, Engine.POSTGRES}),
               "Casting a column to text for a comparison disables pruning.", _duckdb_string_date_compare),
]


def rules_for(engine: Engine) -> list[EngineRule]:
    return [r for r in ENGINE_RULES if engine in r.engines]


def analyze_engine_specific(statements: list[exp.Expr], engine: Engine,
                            stats: Mapping[str, TableStats] | None = None) -> list[Finding]:
    """Findings that only apply to ``engine``."""
    stats = stats or {}
    out: list[Finding] = []
    for stmt in statements:
        for rule in rules_for(engine):
            try:
                out += rule.check(stmt, engine, stats)
            except Exception:  # noqa: BLE001 - a rule must never break the analysis
                continue
    return out


__all__ = ["ENGINE_RULES", "EngineRule", "analyze_engine_specific", "rules_for"]
