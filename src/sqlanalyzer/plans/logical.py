"""Build a logical execution plan from a SQLGlot AST - no database required.

The engines' own ``EXPLAIN`` output is always better, but it needs a connection.
This module derives the plan *shape* a cost-based optimizer would most likely
choose for the written query (scan -> filter -> join -> aggregate -> window ->
sort -> limit), so the plan view and :mod:`sqlanalyzer.plans.explain` work
offline, while typing.

Nothing here is measured: ``est_rows``/``cost``/``time_ms`` stay ``None`` and
``Plan.logical`` is ``True`` so the UI can say where the numbers (don't) come
from. Join algorithms are the ones an engine picks for the given predicate
(equality -> hash, inequality/none -> nested loop), not a promise.
"""

from __future__ import annotations

from sqlglot import exp

from ..dialects import ENGINES, Engine
from ..sqlutil import conjuncts, disjuncts, table_names
from .model import AGG, APPEND, JOIN, OTHER, SCAN, SORT, IdCounter, Plan, PlanNode

WINDOW = "window"  # extra kinds used by logical plans (the UI treats unknown kinds as OTHER)
FILTER = "filter"
LIMIT = "limit"
MATERIALIZE = "materialize"

_SET_OPS = {exp.Union: "Union", exp.Intersect: "Intersect", exp.Except: "Except"}


def build_logical_plan(statement: exp.Expr, engine: Engine = Engine.DUCKDB) -> Plan:
    """Derive the likely operator tree of ``statement`` (one statement, already parsed)."""
    builder = _Builder(engine)
    root = builder.statement(statement)
    plan = Plan(engine, root, raw="", analyzed=False, logical=True)
    plan.stats["source"] = "derived from the SQL text (no database was queried)"
    return plan


class _Builder:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.dialect = ENGINES[engine].sqlglot_dialect
        self.ids = IdCounter()
        self.ctes: dict[str, exp.Expr] = {}
        self.building: set[str] = set()  # guards recursive CTEs

    # ------------------------------------------------------------------ helpers
    def node(self, op: str, kind: str = OTHER, **kwargs) -> PlanNode:
        return PlanNode(self.ids.next(), op, kind, **kwargs)

    def sql(self, node: exp.Expr) -> str:
        return node.sql(dialect=self.dialect)

    def opt_sql(self, node: exp.Expr | None) -> str | None:
        return self.sql(node) if node is not None else None

    # --------------------------------------------------------------- statements
    def statement(self, statement: exp.Expr) -> PlanNode:
        self._collect_ctes(statement)
        if isinstance(statement, (exp.Select, exp.SetOperation, exp.Subquery)):
            return self.query(statement)
        if isinstance(statement, exp.Insert):
            child = self.query(statement.expression) if isinstance(
                statement.expression, (exp.Select, exp.SetOperation)) else self.node("Values", OTHER)
            target = statement.this
            name = self.sql(target)
            return self.node("Insert", OTHER, relation=name, children=[child])
        if isinstance(statement, (exp.Update, exp.Delete)):
            op = "Update" if isinstance(statement, exp.Update) else "Delete"
            table = statement.this if isinstance(statement.this, exp.Table) else statement.find(exp.Table)
            child = self.scan(table) if isinstance(table, exp.Table) else self.node("Source", OTHER)
            where = statement.args.get("where")
            if where is not None:
                child.filter = self.sql(where.this)
                child = self.node("Filter", FILTER, filter=self.sql(where.this), children=[child]) \
                    if child.kind != SCAN else child
            return self.node(op, OTHER, relation=child.relation, children=[child])
        if isinstance(statement, exp.Create):
            inner = statement.expression
            children = [self.query(inner)] if isinstance(inner, (exp.Select, exp.SetOperation)) else []
            return self.node(f"Create {statement.args.get('kind') or 'Table'}".title(), OTHER,
                             relation=self.sql(statement.this), children=children)
        return self.node(type(statement).__name__, OTHER, attrs={"sql": _short(statement.sql())})

    def _collect_ctes(self, statement: exp.Expr) -> None:
        with_ = statement.args.get("with_") or statement.args.get("with")
        if isinstance(with_, exp.With):
            for cte in with_.expressions:
                self.ctes.setdefault(cte.alias.lower(), cte.this)

    # -------------------------------------------------------------------- query
    def query(self, query: exp.Expr) -> PlanNode:
        if isinstance(query, exp.Subquery):
            return self.query(query.this)
        self._collect_ctes(query)
        if isinstance(query, exp.SetOperation):
            return self.set_operation(query)
        if not isinstance(query, exp.Select):
            return self.node(type(query).__name__, OTHER, attrs={"sql": _short(query.sql())})
        return self.select(query)

    def set_operation(self, op: exp.SetOperation) -> PlanNode:
        name = next((v for k, v in _SET_OPS.items() if isinstance(op, k)), "Set operation")
        distinct = bool(op.args.get("distinct"))
        label = f"{name} All" if isinstance(op, exp.Union) and not distinct else name
        children = [self.query(op.this), self.query(op.expression)]
        node = self.node(label, APPEND, children=children,
                         attrs={"duplicates": "removed" if distinct else "kept"})
        return self.tail(op, node)

    def select(self, select: exp.Select) -> PlanNode:
        node = self.from_and_joins(select)
        where = select.args.get("where")
        remaining = [c for c in conjuncts(where) if not _pushed_down(self.sql(c), node)]
        if remaining:
            node = self.node("Filter", FILTER, filter=" AND ".join(self.sql(c) for c in remaining), children=[node])

        group = select.args.get("group")
        aggregates = [self.sql(a) for e in select.expressions for a in e.find_all(exp.AggFunc)
                      if a.find_ancestor(exp.Window) is None]
        if group is not None or aggregates:
            keys = [self.sql(g) for g in group.expressions] if group is not None else []
            attrs = {"group_keys": ", ".join(keys) or "(whole input)",
                     "aggregates": ", ".join(aggregates) or "(none)"}
            op = "Aggregate" if keys else "Aggregate (single group)"
            node = self.node(op, AGG, children=[node], attrs=attrs,
                             condition=", ".join(keys) or None)
        having = select.args.get("having")
        if having is not None:
            node = self.node("Filter (HAVING)", FILTER, filter=self.sql(having.this), children=[node])

        windows = list(select.find_all(exp.Window))
        windows = [w for w in windows if w.find_ancestor(exp.Select) is select]
        if windows:
            specs = sorted({_window_spec(w, self.dialect) for w in windows})
            node = self.node("Window", WINDOW, children=[node],
                             attrs={"functions": ", ".join(sorted({self.sql(w.this) or "window function"
                                                                   for w in windows})),
                                    "partitioning": "; ".join(specs)})
        qualify = select.args.get("qualify")
        if qualify is not None:
            node = self.node("Filter (QUALIFY)", FILTER, filter=self.sql(qualify.this), children=[node])
        if select.args.get("distinct") is not None:
            node = self.node("Distinct", AGG, children=[node])
        return self.tail(select, node)

    def tail(self, query: exp.Expr, node: PlanNode) -> PlanNode:
        """ORDER BY / LIMIT / OFFSET on top of ``node``."""
        order = query.args.get("order")
        limit = query.args.get("limit")
        offset = query.args.get("offset")
        if order is not None:
            keys = ", ".join(self.sql(o) for o in order.expressions)
            op = "Top-N Sort" if limit is not None else "Sort"
            node = self.node(op, SORT, children=[node], condition=keys, attrs={"sort_keys": keys})
        if limit is not None or offset is not None:
            parts = []
            if limit is not None:
                parts.append("limit " + (self.opt_sql(limit.expression) or self.sql(limit) or "?"))
            if offset is not None:
                parts.append("offset " + (self.opt_sql(offset.expression) or self.sql(offset) or "?"))
            node = self.node("Limit", LIMIT, children=[node], attrs={"limit": ", ".join(parts),
                                                                     "ordered": order is not None})
        return node

    # ------------------------------------------------------------ FROM / JOINs
    def from_and_joins(self, select: exp.Select) -> PlanNode:
        from_ = select.args.get("from_") or select.args.get("from")
        where = conjuncts(select.args.get("where"))
        if from_ is None:
            return self.node("Values", OTHER, attrs={"note": "no FROM clause"})
        joins = select.args.get("joins") or []
        single = not joins
        left = self.source(from_.this, where, single)
        for join in joins:
            right = self.source(join.this, where, False)
            left = self.join(join, left, right, where)
        return left

    def join(self, join: exp.Join, left: PlanNode, right: PlanNode, where: list[exp.Expr]) -> PlanNode:
        on = join.args.get("on")
        using = join.args.get("using")
        side = (join.side or "").upper()
        kind = (join.kind or "").upper()
        condition = self.opt_sql(on)
        if using is not None:
            condition = "USING (" + ", ".join(self.sql(u) for u in using) + ")"
        alias = join.this.alias_or_name
        implicit = [c for c in where if _is_equi_join(c) and alias in _aliases(c) and len(_aliases(c)) > 1]
        if condition is None and implicit:
            condition = " AND ".join(self.sql(c) for c in implicit)
            kind = kind or "INNER"
        # single-table predicates in an inner join's ON clause are evaluated at the scan
        if on is not None and not side and right.kind == SCAN:
            local = [c for c in conjuncts(on) if _aliases(c) == {alias} and c.find(exp.Subquery, exp.Select) is None]
            rest = [c for c in conjuncts(on) if c not in local]
            if local:
                pushed = [self.sql(c) for c in local]
                right.filter = " AND ".join(filter(None, [right.filter, *pushed]))
                right.attrs["pushed_filters"] = (right.attrs.get("pushed_filters") or []) + pushed
                condition = " AND ".join(self.sql(c) for c in rest) or None
        equi = bool(using) or (on is not None and any(_is_equi_join(c) for c in conjuncts(on))) or bool(implicit)
        join_type = side or kind or "INNER"
        if join_type == "CROSS" or (condition is None and not using):
            join_type = "CROSS"
        algo = "hash" if equi else "nested_loop"
        op = {"hash": "Hash Join", "nested_loop": "Nested Loop Join"}[algo]
        if join_type == "CROSS":
            op = "Cross Join (Cartesian product)"
        attrs = {"has_condition": condition is not None,
                 "implicit": bool(implicit) and on is None,
                 "equi": equi}
        return self.node(op, JOIN, join_type=join_type, join_algo=algo, condition=condition,
                         children=[left, right], attrs=attrs)

    def source(self, source: exp.Expr, where: list[exp.Expr], single: bool = False) -> PlanNode:
        if isinstance(source, exp.Table):
            return self.scan(source, where, single)
        if isinstance(source, exp.Subquery):
            alias = source.alias_or_name
            inner = self.query(source.this)
            node = self.node("Derived table", MATERIALIZE, alias=alias or None, children=[inner],
                             attrs={"name": alias or "(unnamed)"})
            return node
        if isinstance(source, (exp.Lateral, exp.Unnest)):
            return self.node(type(source).__name__.upper(), OTHER, alias=source.alias_or_name or None,
                             attrs={"sql": _short(source.sql())})
        if isinstance(source, exp.Values):
            return self.node("Values", OTHER, alias=source.alias_or_name or None)
        return self.node(type(source).__name__, OTHER, attrs={"sql": _short(source.sql())})

    def scan(self, table: exp.Table, where: list[exp.Expr] | None = None, single: bool = False) -> PlanNode:
        bare, qualified = table_names(table)
        alias = table.alias_or_name
        cte = self.ctes.get(bare)
        if cte is not None and not table.db and not table.catalog and bare not in self.building:
            self.building.add(bare)
            try:
                inner = self.query(cte)
            finally:
                self.building.discard(bare)
            return self.node(f"CTE {bare}", MATERIALIZE, alias=alias, children=[inner],
                             attrs={"name": bare, "note": "CTEs are inlined by most engines, materialized by some"})
        own = {alias, ""} if single else {alias}
        local = [c for c in (where or []) if _aliases(c) and _aliases(c) <= own
                 and c.find(exp.Subquery, exp.Select) is None]
        node = self.node("Scan", SCAN, relation=qualified, alias=alias if alias != bare else None,
                         filter=" AND ".join(self.sql(c) for c in local) or None)
        node.attrs["pushed_filters"] = [self.sql(c) for c in local]
        return node


# ------------------------------------------------------------------------ helpers
def _aliases(node: exp.Expr) -> set[str]:
    return {c.table for c in node.find_all(exp.Column) if c.table} or {
        "" for _ in [1] if node.find(exp.Column) is not None}


def _is_equi_join(node: exp.Expr) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.EQ) or len(disjuncts(node)) > 1:
        return False
    left = {c.table for c in node.this.find_all(exp.Column) if c.table}
    right = {c.table for c in node.expression.find_all(exp.Column) if c.table}
    return bool(left and right and left != right)


def _pushed_down(sql: str, root: PlanNode) -> bool:
    """True if the predicate already sits on a scan (single-table filter) or a join condition."""
    for node in root.walk():
        if sql in (node.attrs.get("pushed_filters") or []):
            return True
        if node.kind == JOIN and node.condition and sql in node.condition:
            return True
    return False


def _window_spec(window: exp.Window, dialect: str | None = None) -> str:
    parts = []
    if window.args.get("partition_by"):
        parts.append("partition by " + ", ".join(p.sql(dialect=dialect) for p in window.args["partition_by"]))
    order = window.args.get("order")
    if order is not None:
        parts.append("order by " + ", ".join(o.sql(dialect=dialect) for o in order.expressions))
    return " ".join(parts) or "whole result"


def _short(sql: str, limit: int = 80) -> str:
    sql = " ".join(sql.split())
    return sql if len(sql) <= limit else sql[: limit - 1] + "…"
