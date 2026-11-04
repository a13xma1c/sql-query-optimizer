"""A cost model that needs no database.

It annotates the logical plan (:mod:`sqlanalyzer.plans.logical`) with estimated
row counts and abstract cost units, using the classic textbook heuristics of a
cost-based optimizer:

* table sizes from the declared statistics (``orders (rows=2000000): ...``), with a
  documented default when unknown;
* predicate selectivity from the predicate shape (equality, range, LIKE, IN,
  IS NULL, OR/AND composition, key columns);
* join cardinalities from key information (a join on a unique key keeps the
  other side's row count; otherwise a foreign-key style assumption);
* operator costs per engine family: row stores read whole rows, columnar engines
  read only the referenced columns, distributed engines pay for shuffles.

Costs are *relative*: they rank alternatives (the original query vs. a rewrite,
one plan shape vs. another) and point at the dominant operator. They are not
milliseconds. Every assumption the model had to make is listed in
:attr:`CostEstimate.assumptions` so the user can tighten it with statistics.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from ..dialects import ENGINES, Engine
from ..optimizer.schema import TableStats
from ..sqlutil import conjuncts, disjuncts
from .logical import FILTER, LIMIT, MATERIALIZE, WINDOW
from .model import AGG, APPEND, JOIN, SCAN, SORT, Plan, PlanNode

DEFAULT_ROWS = 100_000  # assumed table size when no statistics are declared
COLUMNAR = {Engine.DUCKDB, Engine.TRINO, Engine.SNOWFLAKE}
DISTRIBUTED = {Engine.TRINO, Engine.SNOWFLAKE}
INDEXED = {Engine.POSTGRES, Engine.DUCKDB}  # engines where an equality lookup on a key can use an index

# selectivity defaults (PostgreSQL uses similar constants when it has no statistics)
SEL_EQ = 0.1
SEL_EQ_ID = 0.01  # columns named like ids are usually high-cardinality
SEL_RANGE = 1 / 3
SEL_LIKE_PREFIX = 0.1
SEL_LIKE_WILDCARD = 0.5
SEL_NULL = 0.05
SEL_UNKNOWN = 0.5
SEL_SUBQUERY = 0.5

_ID_LIKE = re.compile(r"(^|_)(id|key|uuid|guid|code|number|no)$", re.I)
_TEMPORAL = re.compile(r"(^|_)(ts|time|timestamp|date|dt|day|created|updated|at)($|_)", re.I)


@dataclass
class CostedNode:
    node_id: int
    op: str
    rows: float
    self_cost: float
    total_cost: float
    note: str = ""


@dataclass
class CostEstimate:
    engine: Engine
    total_cost: float
    output_rows: float
    nodes: list[CostedNode] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    table_rows: dict[str, float] = field(default_factory=dict)  # relation -> rows assumed

    @property
    def hottest(self) -> list[CostedNode]:
        return sorted(self.nodes, key=lambda n: -n.self_cost)[:3]

    def share(self, node_id: int) -> float:
        node = next((n for n in self.nodes if n.node_id == node_id), None)
        return node.self_cost / self.total_cost if node and self.total_cost else 0.0

    def summary(self) -> str:
        text = f"Estimated cost {fmt(self.total_cost)} units, ~{fmt(self.output_rows)} result row(s)"
        if self.hottest:
            top = self.hottest[0]
            text += f"; dominant operator: {top.op} ({self.share(top.node_id):.0%})"
        return text + "."


def fmt(n: float | None) -> str:
    if n is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def cost_delta(before: CostEstimate | None, after: CostEstimate | None) -> str | None:
    """Human text for the change between two estimates (``None`` when not comparable)."""
    if before is None or after is None or not before.total_cost:
        return None
    pct = (after.total_cost - before.total_cost) / before.total_cost * 100
    return f"{fmt(before.total_cost)} -> {fmt(after.total_cost)} cost units ({pct:+.0f}%)"


# ------------------------------------------------------------------------ model
class CostModel:
    def __init__(self, engine: Engine, stats: Mapping[str, TableStats] | None = None,
                 default_rows: int = DEFAULT_ROWS) -> None:
        self.engine = engine
        self.dialect = ENGINES[engine].sqlglot_dialect
        self.stats = {k.lower(): v for k, v in (stats or {}).items()}
        self.default_rows = default_rows
        self.columnar = engine in COLUMNAR
        self.distributed = engine in DISTRIBUTED
        self.assumptions: list[str] = []
        self.table_rows: dict[str, float] = {}
        self.referenced_columns: dict[str, set[str]] = {}  # alias/relation -> columns used by the query

    # ---------------------------------------------------------------- entry
    def estimate(self, plan: Plan, statement: exp.Expr | None = None) -> CostEstimate:
        """Annotate ``plan`` in place (``est_rows``, ``cost``) and return the estimate."""
        self.assumptions = []
        self.table_rows = {}
        self.referenced_columns = _referenced_columns(statement) if statement is not None else {}
        costed: list[CostedNode] = []
        self._visit(plan.root, costed)
        estimate = CostEstimate(self.engine, plan.root.cost or 0.0, plan.root.est_rows or 0.0, costed,
                                list(dict.fromkeys(self.assumptions)), dict(self.table_rows))
        plan.stats["cost_model"] = estimate.summary()
        return estimate

    def _visit(self, node: PlanNode, out: list[CostedNode]) -> None:
        for child in node.children:
            self._visit(child, out)
        rows, self_cost, note = self._node(node)
        rows = max(rows, 0.0)
        node.est_rows = rows
        node.cost = self_cost + sum(c.cost or 0.0 for c in node.children)
        node.attrs["model_cost"] = round(self_cost, 1)
        if note:
            node.attrs["model_note"] = note
        out.append(CostedNode(node.id, node.title, rows, self_cost, node.cost, note))

    # ------------------------------------------------------------- operators
    def _node(self, node: PlanNode) -> tuple[float, float, str]:
        kind = node.kind
        inputs = [c.est_rows or 0.0 for c in node.children]
        rows_in = inputs[0] if inputs else 0.0
        if kind == SCAN:
            return self._scan(node)
        if kind == FILTER:
            sel, note = self._selectivity(node.filter)
            return rows_in * sel, rows_in * 0.1, note
        if kind == JOIN:
            return self._join(node, inputs)
        if kind == AGG:
            if node.op.lower().startswith("distinct"):
                rows = _groups(rows_in, 1, "distinct")
                return rows, rows_in * 1.2, "DISTINCT hashes every input row"
            keys = node.attrs.get("group_keys") or ""
            if keys in ("", "(whole input)"):
                return 1.0, rows_in * 0.5, "single-group aggregate"
            n_keys = len([k for k in keys.split(",") if k.strip()])
            rows = _groups(rows_in, n_keys, "group by")
            self.assumptions.append(f"GROUP BY output estimated as input^0.7 per key ({fmt(rows)} groups)")
            return rows, rows_in * 1.2, "hash aggregate"
        if kind == WINDOW:
            return rows_in, rows_in * (math.log2(max(rows_in, 2)) * 0.5 + 1), "partition + sort for window functions"
        if kind == SORT:
            if node.op.lower().startswith("top"):
                return rows_in, rows_in * 1.5, "top-N keeps a small heap instead of sorting everything"
            cost = rows_in * math.log2(max(rows_in, 2)) * 0.6
            if self.distributed:
                cost += rows_in * 0.5  # a global ORDER BY gathers everything on one node
            return rows_in, cost, "full sort (n log n)"
        if kind == LIMIT:
            limit = _int_in(node.attrs.get("limit", ""))
            rows = min(rows_in, float(limit)) if limit is not None else rows_in
            return rows, 1.0, ""
        if kind == MATERIALIZE:
            factor = 0.3 if self.engine is Engine.POSTGRES else 0.1
            return rows_in, rows_in * factor, "derived table / CTE materialisation"
        if kind == APPEND:
            rows = sum(inputs)
            if node.attrs.get("duplicates") == "removed":
                return rows * 0.9, rows * 1.2, "UNION de-duplicates the combined rows"
            return rows, rows * 0.05, ""
        if node.op.lower() in ("insert", "update", "delete"):
            return rows_in, rows_in * 2.0, "writes every affected row"
        return rows_in, rows_in * 0.05 if inputs else 1.0, ""

    def _scan(self, node: PlanNode) -> tuple[float, float, str]:
        relation = (node.relation or "").lower()
        stats = self._stats(relation)
        if stats is not None and stats.rows is not None:
            rows = float(stats.rows)
        else:
            rows = float(self.default_rows)
            self.assumptions.append(f"no row count declared for '{node.relation}': assumed {fmt(rows)} rows "
                                    "(add '(rows=N)' to the schema hints)")
        self.table_rows[node.relation or relation] = rows
        sel, note = self._selectivity(node.filter, stats, node.alias or node.relation)
        out_rows = rows * sel
        width = self._width(node, stats)
        unit = (0.35 * width if self.columnar else 1.0)
        cost = rows * unit
        how = f"reads {fmt(rows)} rows"
        if self.columnar:
            how += f" ({width:.0%} of the columns)"
        access = self._access_path(node.filter, stats)
        # the engine picks the cheapest access path it has: a full scan is always possible
        candidates = {"scan": (cost, how)}
        if access == "seek":
            candidates["seek"] = (math.log2(max(rows, 2)) * 4 + out_rows * 2, f"index seek, ~{fmt(out_rows)} rows")
        if access in ("seek", "prune"):
            read = rows * min(1.0, sel + 0.05)
            path = "index range scan" if self.engine is Engine.POSTGRES else "partition/zone-map pruning"
            candidates["prune"] = (rows * unit * min(1.0, sel + 0.05), f"{path}, ~{fmt(read)} rows read")
        chosen = min(candidates, key=lambda k: candidates[k][0])
        cost, how = candidates[chosen]
        node.attrs["access"] = chosen if access != "not-sargable" else access
        if access == "not-sargable":
            how += "; the filter wraps the column, so no index or pruning can help"
        return out_rows, cost, how + (f"; {note}" if note else "")

    def _join(self, node: PlanNode, inputs: list[float]) -> tuple[float, float, str]:
        left = inputs[0] if inputs else 0.0
        right = inputs[1] if len(inputs) > 1 else 0.0
        jt = (node.join_type or "INNER").upper()
        if jt == "CROSS" or not node.condition:
            self.assumptions.append("Cartesian product estimated as |left| x |right| rows")
            return left * right, left * right, "Cartesian product"
        if node.join_algo == "nested_loop":
            rows = left * right * SEL_RANGE
            return rows, left * right * 0.02 + rows, "nested loop: every pair is compared"
        keys = _join_key_columns(node.condition, self.dialect)
        rows = self._equi_join_rows(node, left, right, keys)
        build, probe = min(left, right), max(left, right)
        cost = build * 1.5 + probe * 1.0
        note = f"hash join: build {fmt(build)}, probe {fmt(probe)}"
        if self.distributed:
            cost += (left + right) * 0.3
            note += ", shuffle"
        if jt in ("LEFT", "FULL"):
            rows = max(rows, left)
        if jt == "RIGHT":
            rows = max(rows, right)
        return rows, cost, note

    def _equi_join_rows(self, node: PlanNode, left: float, right: float, keys: list[tuple[str, str]]) -> float:
        """Rows of an equi-join: a unique key on one side keeps the other side's rows."""
        unique_sides: set[int] = set()
        for table, column in keys:
            side = _side_of(node, table)
            stats = self._stats(self._relation_of(node, table) or table)
            if side is not None and stats is not None and column in stats.unique:
                unique_sides.add(side)
        if unique_sides == {0, 1}:
            return min(left, right)
        if 0 in unique_sides:
            return right
        if 1 in unique_sides:
            return left
        self.assumptions.append("join cardinality estimated as the larger input (foreign-key assumption)")
        return max(left, right)

    def _relation_of(self, node: PlanNode, alias: str) -> str | None:
        for scan in node.walk():
            if scan.kind == SCAN and alias in ((scan.alias or "").lower(), (scan.relation or "").lower(),
                                               (scan.relation or "").lower().split(".")[-1]):
                return scan.relation
        return None

    # ------------------------------------------------------------- helpers
    def _stats(self, relation: str) -> TableStats | None:
        relation = relation.lower()
        if relation in self.stats:
            return self.stats[relation]
        bare = relation.split(".")[-1]
        for name, stats in self.stats.items():
            if name.split(".")[-1] == bare:
                return stats
        return None

    def _width(self, node: PlanNode, stats: TableStats | None) -> float:
        """Fraction of the table's columns the query touches (columnar engines read only those)."""
        if stats is None or not stats.columns:
            return 0.5
        used = self.referenced_columns.get((node.alias or node.relation or "").lower()) \
            or self.referenced_columns.get((node.relation or "").lower().split(".")[-1])
        if used is None:
            return 0.5
        if "*" in used:
            return 1.0
        return max(0.05, min(1.0, len(used & set(stats.columns)) / len(stats.columns)))

    def _selectivity(self, filter_sql: str | None, stats: TableStats | None = None,
                     alias: str | None = None) -> tuple[float, str]:
        if not filter_sql:
            return 1.0, ""
        try:
            condition = sqlglot.parse_one(filter_sql, read=self.dialect)
        except Exception:  # noqa: BLE001 - unknown syntax: keep a neutral estimate
            return SEL_UNKNOWN, "selectivity unknown"
        sel = self._sel(_merge_ranges(condition), stats)
        return sel, f"selectivity {sel:.3g}"

    def _sel(self, node: exp.Expr, stats: TableStats | None) -> float:
        while isinstance(node, exp.Paren):
            node = node.this
        if isinstance(node, exp.And):
            return self._sel(node.this, stats) * self._sel(node.expression, stats)
        if isinstance(node, exp.Or):
            parts = [self._sel(d, stats) for d in disjuncts(node)]
            total = 0.0
            for p in parts:
                total = total + p - total * p
            return total
        if isinstance(node, exp.Not):
            return 1.0 - self._sel(node.this, stats)
        column = node.find(exp.Column)
        name = column.name.lower() if column is not None else ""
        if isinstance(node, exp.EQ):
            return self._eq_sel(name, stats)
        if isinstance(node, exp.NEQ):
            return 1.0 - self._eq_sel(name, stats)
        if isinstance(node, exp.Between):
            return SEL_RANGE * 0.3  # both bounds: a narrow interval
        if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE)):
            return SEL_RANGE
        if isinstance(node, exp.In):
            if node.args.get("query") is not None:
                return SEL_SUBQUERY
            return min(1.0, len(node.expressions) * self._eq_sel(name, stats))
        if isinstance(node, (exp.Like, exp.ILike)):
            pattern = node.expression
            text = pattern.this if isinstance(pattern, exp.Literal) else "%"
            return SEL_LIKE_WILDCARD if text[:1] in ("%", "_") else SEL_LIKE_PREFIX
        if isinstance(node, exp.Is):
            return SEL_NULL if isinstance(node.expression, exp.Null) else SEL_EQ
        if isinstance(node, exp.Exists):
            return SEL_SUBQUERY
        if isinstance(node, exp.Boolean):
            return 1.0 if node.this else 0.0
        return SEL_UNKNOWN

    def _eq_sel(self, column: str, stats: TableStats | None) -> float:
        if stats is not None:
            if column in stats.unique and stats.rows:
                return 1.0 / stats.rows
            if column in stats.ndv and stats.ndv[column] > 0:
                return 1.0 / stats.ndv[column]
        return SEL_EQ_ID if _ID_LIKE.search(column or "") else SEL_EQ

    def _access_path(self, filter_sql: str | None, stats: TableStats | None) -> str:
        """How the scan can be executed: ``seek`` (index), ``prune``, ``scan`` or ``not-sargable``."""
        if not filter_sql:
            return "scan"
        try:
            condition = sqlglot.parse_one(filter_sql, read=self.dialect)
        except Exception:  # noqa: BLE001
            return "scan"
        best = "scan"
        for pred in conjuncts(condition):
            column, kind = _sargable_column(pred)
            if kind == "not-sargable":
                best = "not-sargable" if best == "scan" else best
                continue
            if column is None:
                continue
            if stats is None or not stats.prunable(column):
                if self.columnar and kind in ("eq", "range") and _TEMPORAL.search(column):
                    # columnar storage is usually loaded in time order: min/max zone maps prune on it
                    self.assumptions.append(f"'{column}' assumed to be in load order (zone-map pruning applies); "
                                            "mark it 'indexed'/'sorted' in the schema hints to confirm")
                    best = "prune"
                continue
            if kind == "eq" and self.engine in INDEXED and (
                    column in stats.unique or (self.engine is Engine.POSTGRES and column in stats.indexed)):
                return "seek"
            if kind in ("eq", "range"):
                best = "prune"
        return best


# ---------------------------------------------------------------------- helpers
def _sargable_column(pred: exp.Expr) -> tuple[str | None, str]:
    """(column, 'eq' | 'range' | 'other' | 'not-sargable') for a simple predicate."""
    while isinstance(pred, exp.Paren):
        pred = pred.this
    if isinstance(pred, (exp.EQ, exp.In, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between, exp.Like)):
        sides = [pred.this] + ([pred.expression] if not isinstance(pred, (exp.In, exp.Between)) else [])
        for side in sides:
            if isinstance(side, exp.Column):
                other_has_column = any(s.find(exp.Column) is not None for s in sides if s is not side)
                if other_has_column:
                    return side.name.lower(), "other"
                if isinstance(pred, exp.Like):
                    pattern = pred.expression
                    if isinstance(pattern, exp.Literal) and pattern.this[:1] not in ("%", "_"):
                        return side.name.lower(), "range"
                    return side.name.lower(), "other"
                return side.name.lower(), "eq" if isinstance(pred, (exp.EQ, exp.In)) else "range"
            if (inner := side.find(exp.Column)) is not None and isinstance(side, (exp.Func, exp.Cast, exp.Binary)):
                return inner.name.lower(), "not-sargable"
    return None, "other"


def _merge_ranges(condition: exp.Expr) -> exp.Expr:
    """Collapse both bounds of an interval on one column into one range predicate."""
    parts = conjuncts(condition)
    if len(parts) < 2:
        return condition
    by_column: dict[str, list[exp.Expr]] = {}
    for pred in parts:
        if isinstance(pred, (exp.GT, exp.GTE, exp.LT, exp.LTE)) and isinstance(pred.this, exp.Column) \
                and pred.expression.find(exp.Column) is None:
            by_column.setdefault(pred.this.name.lower(), []).append(pred)
    merged: list[exp.Expr] = []
    skip: set[int] = set()
    for preds in by_column.values():
        lower = [p for p in preds if isinstance(p, (exp.GT, exp.GTE))]
        upper = [p for p in preds if isinstance(p, (exp.LT, exp.LTE))]
        if lower and upper:
            skip.update(id(p) for p in preds)
            merged.append(exp.Between(this=preds[0].this.copy(), low=exp.Literal.number(0), high=exp.Literal.number(1)))
    if not merged:
        return condition
    keep = [p.copy() for p in parts if id(p) not in skip] + merged
    return exp.and_(*keep, copy=False)


def _groups(rows: float, keys: int, _what: str) -> float:
    if rows <= 1:
        return rows
    return min(rows, max(1.0, rows ** min(0.95, 0.7 + 0.05 * (keys - 1))))


def _side_of(join: PlanNode, alias: str) -> int | None:
    """Which input (0 = left, 1 = right) of ``join`` scans the table known as ``alias``."""
    for index, child in enumerate(join.children[:2]):
        for scan in child.walk():
            names = {(scan.alias or "").lower(), (scan.relation or "").lower(),
                     (scan.relation or "").lower().split(".")[-1]}
            if scan.kind == SCAN and alias in names:
                return index
    return None


def _int_in(text: str) -> int | None:
    m = re.search(r"limit\s+(\d+)", text)
    return int(m.group(1)) if m else None


def _join_key_columns(condition: str, dialect: str) -> list[tuple[str, str]]:
    try:
        parsed = sqlglot.parse_one(condition, read=dialect)
    except Exception:  # noqa: BLE001
        return []
    keys: list[tuple[str, str]] = []
    for pred in conjuncts(parsed):
        if isinstance(pred, exp.EQ):
            for side in (pred.this, pred.expression):
                if isinstance(side, exp.Column):
                    keys.append((side.table.lower(), side.name.lower()))
    return keys


def _referenced_columns(statement: exp.Expr) -> dict[str, set[str]]:
    """alias (and bare table name) -> column names the statement references."""
    out: dict[str, set[str]] = {}
    aliases: dict[str, str] = {}
    for table in statement.find_all(exp.Table):
        aliases[table.alias_or_name.lower()] = table.name.lower()
    for column in statement.find_all(exp.Column):
        key = column.table.lower() if column.table else ""
        out.setdefault(key, set()).add(column.name.lower())
    for star in statement.find_all(exp.Star):
        parent = star.parent
        if isinstance(parent, exp.Column) and parent.table:
            out.setdefault(parent.table.lower(), set()).add("*")
        elif isinstance(parent, exp.Select):
            for alias in aliases:
                out.setdefault(alias, set()).add("*")
    # unqualified columns may belong to any table: count them for every table
    unqualified = out.pop("", set())
    for alias, table_name in aliases.items():
        out.setdefault(alias, set()).update(unqualified)
        out.setdefault(table_name, set()).update(out[alias])
    return out


def estimate_cost(plan: Plan, engine: Engine, stats: Mapping[str, TableStats] | None = None,
                  statement: exp.Expr | None = None) -> CostEstimate:
    """Convenience wrapper: annotate a logical plan and return its estimate."""
    return CostModel(engine, stats).estimate(plan, statement)
