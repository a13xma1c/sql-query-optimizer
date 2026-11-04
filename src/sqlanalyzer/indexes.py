"""Index and physical-design recommendations derived from the query (no database needed).

For every physical table the query touches, the advisor collects how the table is
accessed - equality and range predicates, join keys, ORDER BY / GROUP BY columns,
function-wrapped (non-sargable) columns, case-insensitive LIKE - and turns that
into the engine's own physical-design vocabulary:

* PostgreSQL: B-tree indexes (equality columns first, then one range column),
  expression indexes, ``pg_trgm`` GIN indexes for ``lower(col) LIKE`` / ``%x%``;
* DuckDB: ART indexes for selective point lookups, sort order for zone-map pruning;
* Trino (Iceberg/Hive): partitioning and ``sorted_by`` table properties;
* Snowflake: clustering keys and search optimization.

Declared statistics (``orders (rows=2000000): id int pk, ts timestamp indexed``)
suppress recommendations for columns that are already keys/indexed and drive the
priority. Recommendations are *candidates* to test - they list the predicates
that motivated them and the static findings they address.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from sqlglot import exp

from .dialects import Engine
from .optimizer.schema import TableStats
from .plans.model import Plan
from .sqlutil import column_owner, conjuncts, is_wrapped, real_tables, select_of, select_scopes, table_names

LARGE_TABLE = 500_000
_ID_LIKE = re.compile(r"(^|_)(id|key|uuid|guid|code|number|no)$", re.I)
_RANGE = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)
_TEMPORAL_NAME = re.compile(r"(^|_)(ts|time|timestamp|date|dt|day|created|updated|at)($|_)", re.I)


@dataclass(frozen=True)
class IndexRecommendation:
    engine: Engine
    table: str
    columns: tuple[str, ...]
    kind: str  # btree | expression | trigram | art | sort-order | partition | sorted-by | clustering | search-optimization
    ddl: str
    reason: str
    priority: str  # high | medium | low
    fixes: tuple[str, ...] = ()
    table_rows: float | None = None

    @property
    def key(self) -> tuple[str, str, tuple[str, ...]]:
        return (self.table.lower(), self.kind, tuple(c.lower() for c in self.columns))

    @property
    def title(self) -> str:
        return f"{self.kind} on {self.table} ({', '.join(self.columns)})"


@dataclass
class _Access:
    """How one query block accesses one table."""

    table: exp.Table
    alias: str
    eq: list[str] = field(default_factory=list)
    range: list[str] = field(default_factory=list)
    join: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)
    group: list[str] = field(default_factory=list)
    wrapped: list[tuple[str, str]] = field(default_factory=list)  # (column, expression sql)
    ci_like: list[str] = field(default_factory=list)  # lower(col) LIKE / ILIKE
    wildcard_like: list[str] = field(default_factory=list)  # col LIKE '%x%'
    predicates: list[str] = field(default_factory=list)


def recommend_indexes(statements: list[exp.Expr], engine: Engine,
                      stats: Mapping[str, TableStats] | None = None, plan: Plan | None = None,
                      dialect: str | None = None) -> list[IndexRecommendation]:
    stats = {k.lower(): v for k, v in (stats or {}).items()}
    scan_rows = _scan_rows(plan)
    out: dict[tuple, IndexRecommendation] = {}
    for statement in statements:
        for scope in select_scopes(statement):
            for access in _collect(scope, dialect):
                table_stats = _stats_for(stats, access.table)
                rows = _rows_for(access, table_stats, scan_rows)
                for rec in _recommend(access, engine, table_stats, rows, dialect):
                    if rec.key not in out:
                        out[rec.key] = rec
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out.values(), key=lambda r: (order[r.priority], r.table, r.kind))


# ------------------------------------------------------------------ collection
def _collect(scope, dialect: str | None) -> list[_Access]:
    sel = select_of(scope)
    tables = real_tables(scope)
    if not tables:
        return []
    accesses = {alias: _Access(table, alias) for alias, table in tables.items()}

    def owner(column: exp.Column) -> _Access | None:
        alias = column_owner(column, tables)
        return accesses.get(alias) if alias else None

    preds = conjuncts(sel.args.get("where"))
    for join in sel.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None and (join.side or "").upper() not in ("LEFT", "RIGHT", "FULL"):
            preds += conjuncts(on)
        elif on is not None:
            preds += [c for c in conjuncts(on) if _is_equi(c, tables)]  # join keys still matter for outer joins
    for pred in preds:
        _classify(pred, owner, tables, dialect)

    order = sel.args.get("order")
    if order is not None:
        for ordered in order.expressions:
            col = ordered.this
            if isinstance(col, exp.Column) and (acc := owner(col)) is not None:
                acc.order.append(col.name)
    group = sel.args.get("group")
    if group is not None:
        for g in group.expressions:
            if isinstance(g, exp.Column) and (acc := owner(g)) is not None:
                acc.group.append(g.name)
    return list(accesses.values())


def _is_equi(pred: exp.Expr, tables: dict[str, exp.Table]) -> bool:
    while isinstance(pred, exp.Paren):
        pred = pred.this
    if not isinstance(pred, exp.EQ):
        return False
    left = {column_owner(c, tables) for c in pred.this.find_all(exp.Column)} - {None}
    right = {column_owner(c, tables) for c in pred.expression.find_all(exp.Column)} - {None}
    return bool(left and right and left != right)


def _classify(pred: exp.Expr, owner, tables: dict[str, exp.Table], dialect: str | None) -> None:
    while isinstance(pred, exp.Paren):
        pred = pred.this
    if isinstance(pred, (exp.And, exp.Or)):
        return  # composite predicates are not index candidates on their own
    sql = pred.sql(dialect=dialect)
    if _is_equi(pred, tables):
        for side in (pred.this, pred.expression):
            if isinstance(side, exp.Column) and (acc := owner(side)) is not None:
                acc.join.append(side.name)
                acc.predicates.append(sql)
            elif side.find(exp.Column) is not None and isinstance(side, (exp.Func, exp.Cast, exp.Binary)):
                col = side.find(exp.Column)
                if col is not None and (acc := owner(col)) is not None:
                    acc.wrapped.append((col.name, side.sql(dialect=dialect)))
        return
    if isinstance(pred, (exp.EQ, exp.In, *_RANGE, exp.Like, exp.ILike)):
        sides = [pred.this] + ([] if isinstance(pred, (exp.In, exp.Between)) else [pred.expression])
        for side in sides:
            if any(s is not side and s.find(exp.Column) is not None for s in sides):
                continue
            column = side if isinstance(side, exp.Column) else None
            if column is None and side.find(exp.Column) is not None:
                col = side.find(exp.Column)
                acc = owner(col)
                if acc is None:
                    continue
                if isinstance(pred, (exp.Like, exp.ILike)) and isinstance(side, (exp.Lower, exp.Upper)):
                    acc.ci_like.append(col.name)
                elif is_wrapped(col, pred):
                    acc.wrapped.append((col.name, side.sql(dialect=dialect)))
                acc.predicates.append(sql)
                continue
            if column is None:
                continue
            acc = owner(column)
            if acc is None:
                continue
            acc.predicates.append(sql)
            if isinstance(pred, (exp.EQ, exp.In)):
                acc.eq.append(column.name)
            elif isinstance(pred, exp.ILike):
                acc.ci_like.append(column.name)
            elif isinstance(pred, exp.Like):
                pattern = pred.expression
                text = pattern.this if isinstance(pattern, exp.Literal) else "%"
                (acc.wildcard_like if text[:1] in ("%", "_") else acc.range).append(column.name)
            else:
                acc.range.append(column.name)


# --------------------------------------------------------------- recommendation
def _stats_for(stats: Mapping[str, TableStats], table: exp.Table) -> TableStats | None:
    bare, qualified = table_names(table)
    return stats.get(qualified) or stats.get(bare) or next(
        (s for name, s in stats.items() if name.split(".")[-1] == bare), None)


def _scan_rows(plan: Plan | None) -> dict[str, float]:
    if plan is None:
        return {}
    rows: dict[str, float] = {}
    for node in plan.nodes():
        if node.kind in ("scan", "index_scan") and node.relation:
            # the rows a scan had to read: estimated table size offline, or actual/estimated input
            size = node.est_total_rows or node.est_rows
            removed = node.attrs.get("Rows Removed by Filter")
            if node.actual_rows is not None:
                size = node.actual_rows + (float(removed) if removed else 0.0)
            elif plan.logical and node.attrs.get("model_cost") is not None and node.est_rows is not None:
                size = None  # the logical scan node reports output rows; table size comes from stats
            if size:
                rows[node.relation.lower()] = max(rows.get(node.relation.lower(), 0.0), float(size))
    return rows


def _rows_for(access: _Access, stats: TableStats | None, scan_rows: Mapping[str, float]) -> float | None:
    if stats is not None and stats.rows:
        return float(stats.rows)
    bare, qualified = table_names(access.table)
    for key, value in scan_rows.items():
        if key == qualified or key.split(".")[-1] == bare:
            return value
    return None


def _dedupe(columns: list[str]) -> list[str]:
    return list(dict.fromkeys(c for c in columns))


def _recommend(access: _Access, engine: Engine, stats: TableStats | None, rows: float | None,
               dialect: str | None) -> list[IndexRecommendation]:
    bare, qualified = table_names(access.table)
    table = access.table.sql(dialect=dialect).split(" AS ")[0]
    covered = set(stats.unique) | set(stats.indexed) if stats else set()
    eq = [c for c in _dedupe(access.eq) if c.lower() not in covered]
    rng = [c for c in _dedupe(access.range) if c.lower() not in covered]
    join = [c for c in _dedupe(access.join) if c.lower() not in covered and c not in eq]
    order = [c for c in _dedupe(access.order + access.group) if c.lower() not in covered]
    wrapped = [(c, e) for c, e in _dedupe_pairs(access.wrapped) if c.lower() not in covered]
    wrapped_cols = _dedupe([c for c, _ in wrapped])
    out: list[IndexRecommendation] = []
    big = rows is None or rows >= LARGE_TABLE
    filtered = bool(eq or rng or wrapped_cols)
    priority_filter = "high" if big and filtered else "medium" if filtered else "low"

    def why(cols: list[str]) -> str:
        wanted = {c.lower() for c in cols}
        hits = [p for p in _dedupe(access.predicates)
                if any(re.search(rf"\b{re.escape(c)}\b", p, re.I) for c in wanted)]
        return "; ".join(hits[:3])

    if engine is Engine.POSTGRES:
        cols = eq + rng[:1]
        if not cols and order and not join:
            cols = order
        if cols:
            out.append(_rec(engine, table, cols, "btree", _pg_index(table, cols),
                            f"equality/range predicates on {', '.join(cols)}: {why(cols)}"
                            + ("; ORDER BY can walk the index" if order and set(order) <= set(cols + rng) else ""),
                            priority_filter, (), rows))
        for col in wrapped_cols:
            out.append(_rec(engine, table, [col], "btree", _pg_index(table, [col]),
                            f"'{col}' is only filtered through a function ({why([col])}); after the Optimize tab "
                            "rewrites that as a range, this index serves the predicate",
                            "high" if big else "medium", ("non-sargable-predicate",), rows))
        for col in join:
            out.append(_rec(engine, table, [col], "btree", _pg_index(table, [col]),
                            f"join key '{col}' (nested loop lookups and FK checks): {why([col])}",
                            "medium" if big else "low", (), rows))
        for _col, expression in wrapped:
            bare_expr = _unqualified(expression, dialect)
            out.append(_rec(engine, table, [bare_expr], "expression", _pg_index(table, [f"({bare_expr})"]),
                            f"alternative if the query cannot be rewritten: index the expression '{bare_expr}'",
                            "low", ("non-sargable-predicate",), rows))
        for col in _dedupe(access.ci_like + access.wildcard_like):
            if col.lower() in covered:
                continue
            out.append(_rec(engine, table, [col], "trigram",
                            f"CREATE EXTENSION IF NOT EXISTS pg_trgm;\nCREATE INDEX ix_{bare}_{col}_trgm ON {table} "
                            f"USING gin ({col} gin_trgm_ops);",
                            f"case-insensitive or wildcard LIKE on '{col}' cannot use a B-tree", "medium",
                            ("leading-wildcard",), rows))
    elif engine is Engine.DUCKDB:
        for col in eq:
            selective = _ID_LIKE.search(col) or (stats and stats.ndv.get(col.lower(), 0) > 1000)
            if selective:
                out.append(_rec(engine, table, [col], "art", f"CREATE INDEX ix_{bare}_{col} ON {table} ({col});",
                                f"point lookups on '{col}' (ART indexes help selective equality predicates only)",
                                "high" if big else "medium", (), rows))
        sort_cols = _dedupe(wrapped_cols + rng + eq)
        if sort_cols:
            out.append(_rec(engine, table, sort_cols[:2], "sort-order",
                            f"-- keep {table} physically ordered by {', '.join(sort_cols[:2])}, e.g.\n"
                            f"CREATE TABLE {bare}_sorted AS SELECT * FROM {table} ORDER BY {', '.join(sort_cols[:2])};",
                            f"range/equality predicates on {', '.join(sort_cols[:2])} prune row groups through "
                            "min/max zone maps when the data is ordered"
                            + (f" (rewrite '{wrapped[0][1]}' as a range first)" if wrapped else ""),
                            priority_filter, ("non-sargable-predicate",) if wrapped else (), rows))
    elif engine is Engine.TRINO:
        temporal = [c for c in wrapped_cols + rng + eq if _TEMPORAL_NAME.search(c)]
        if temporal:
            col = temporal[0]
            note = f" (rewrite '{wrapped[0][1]}' as a range first)" if col in wrapped_cols else ""
            out.append(_rec(engine, table, [col], "partition",
                            f"ALTER TABLE {table} SET PROPERTIES partitioning = ARRAY['day({col})'];  -- Iceberg\n"
                            f"-- Hive: PARTITIONED BY ({col}) and filter with literals",
                            f"predicates on '{col}' can prune partitions instead of scanning the table{note}",
                            priority_filter, ("no-partition-pruning", "non-sargable-predicate") if note else
                            ("no-partition-pruning",), rows))
        keys = [c for c in join + eq if c not in temporal]
        if keys:
            out.append(_rec(engine, table, keys[:2], "sorted-by",
                            f"ALTER TABLE {table} SET PROPERTIES sorted_by = ARRAY['{keys[0]}'];  -- Iceberg\n"
                            f"-- Hive: bucketed_by = ARRAY['{keys[0]}'] for co-located joins",
                            f"equality/join predicates on {', '.join(keys[:2])} benefit from sorted/bucketed files",
                            "medium", (), rows))
    elif engine is Engine.SNOWFLAKE:
        cluster = _dedupe(wrapped_cols + rng + eq + join)[:3]
        if cluster:
            note = f" (rewrite '{wrapped[0][1]}' as a range first)" if wrapped else ""
            out.append(_rec(engine, table, cluster, "clustering",
                            f"ALTER TABLE {table} CLUSTER BY ({', '.join(cluster)});",
                            f"filters/joins on {', '.join(cluster)} prune micro-partitions when the table is "
                            f"clustered on them (worth it above ~1 TB or for very selective filters){note}",
                            priority_filter, ("poor-partition-pruning", "non-sargable-predicate") if wrapped
                            else ("poor-partition-pruning",), rows))
        for col in eq:
            if _ID_LIKE.search(col) or (stats and stats.ndv.get(col.lower(), 0) > 10_000):
                out.append(_rec(engine, table, [col], "search-optimization",
                                f"ALTER TABLE {table} ADD SEARCH OPTIMIZATION ON EQUALITY({col});",
                                f"point lookups on high-cardinality '{col}'", "medium", (), rows))
    return out


def _unqualified(expression: str, dialect: str | None) -> str:
    """``CAST(o.ts AS DATE)`` -> ``CAST(ts AS DATE)`` (index expressions cannot use aliases)."""
    try:
        import sqlglot

        node = sqlglot.parse_one(expression, read=dialect)
        for column in node.find_all(exp.Column):
            column.set("table", None)
        return node.sql(dialect=dialect)
    except Exception:  # noqa: BLE001
        return re.sub(r"\b\w+\.(\w+)", r"\1", expression)


def _dedupe_pairs(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out = []
    for col, expression in pairs:
        if expression not in seen:
            seen.add(expression)
            out.append((col, expression))
    return out


def _pg_index(table: str, cols: list[str]) -> str:
    bare = table.split(".")[-1].strip('"')
    name = "ix_" + bare + "_" + "_".join(re.sub(r"\W+", "", c.lower()) for c in cols)
    return f"CREATE INDEX {name[:60]} ON {table} ({', '.join(cols)});"


def _rec(engine: Engine, table: str, cols: list[str], kind: str, ddl: str, reason: str, priority: str,
         fixes: tuple[str, ...], rows: float | None) -> IndexRecommendation:
    return IndexRecommendation(engine, table, tuple(cols), kind, ddl, reason, priority, fixes, rows)
