"""Snowflake ``EXPLAIN USING JSON`` parser."""

from __future__ import annotations

import json

from ..dialects import Engine
from .model import AGG, JOIN, OTHER, SCAN, SORT, IdCounter, Plan, PlanNode, PlanParseError, to_float

_STRUCTURAL = {"id", "parentOperators", "operation", "expressions", "objects", "alias"}


def _kind(op: str) -> str:
    if op == "TableScan":
        return SCAN
    if "Join" in op:
        return JOIN
    if op in ("Aggregate", "GroupingSets"):
        return AGG
    if op in ("Sort", "SortWithLimit"):
        return SORT
    if op in ("WithReference", "WithClause"):
        return OTHER
    if op in ("UnionAll", "Union"):
        return OTHER
    return OTHER


def parse_snowflake(raw: str | dict) -> Plan:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(data, dict) or "Operations" not in data:
        raise PlanParseError("Not a Snowflake EXPLAIN USING JSON document (no 'Operations').")

    ops: list[dict] = []
    for item in data["Operations"]:
        ops.extend(item if isinstance(item, list) else [item])
    if not ops:
        raise PlanParseError("Snowflake plan has no operations.")

    # Snowflake ids restart per step; make them unique by their position.
    nodes: dict[int, PlanNode] = {}
    parents: dict[int, list[int]] = {}
    ids = IdCounter()
    for pos, d in enumerate(ops):
        op = d.get("operation", "?")
        node = PlanNode(id=ids.next(), op=op, kind=_kind(op))
        objects = d.get("objects") or []
        if node.kind == SCAN and objects:
            node.relation = str(objects[0])
            node.alias = d.get("alias")
        if node.kind == JOIN:
            _fill_join(node, op, d.get("expressions") or [])
        exprs = d.get("expressions") or []
        if exprs and node.kind != JOIN:
            node.attrs["Expressions"] = ", ".join(map(str, exprs))
        if "rowCount" in d:
            node.est_rows = to_float(d["rowCount"])
        for key in ("partitionsTotal", "partitionsAssigned", "bytesAssigned"):
            if key in d:
                node.attrs[key] = d[key]
        for k, v in d.items():
            if k not in _STRUCTURAL and k not in node.attrs and k != "rowCount":
                node.attrs[k] = v if isinstance(v, (int, float, str)) else json.dumps(v)
        nodes[pos] = node
        parents[pos] = d.get("parentOperators") or []

    # Resolve parent ids within the same step (list) where they were declared.
    pos = 0
    roots: list[PlanNode] = []
    for item in data["Operations"]:
        group = item if isinstance(item, list) else [item]
        by_local = {d.get("id"): nodes[pos + i] for i, d in enumerate(group)}
        for i, _d in enumerate(group):
            node = nodes[pos + i]
            pids = parents[pos + i]
            if not pids:
                roots.append(node)
            for pid in pids:
                if pid in by_local:
                    by_local[pid].children.append(node)
        pos += len(group)

    # A step's leading op is the consumer of the previous steps' roots only when
    # they are linked via WithReference; keep every parentless op as a root.
    root = roots[0] if len(roots) == 1 else PlanNode(ids.next(), "Plan", children=roots)
    g = data.get("GlobalStats", {}) or {}
    return Plan(Engine.SNOWFLAKE, root, raw if isinstance(raw, str) else json.dumps(data), False, dict(g))


_JOIN_TYPES = {"inner": "INNER", "leftouter": "LEFT", "rightouter": "RIGHT", "fullouter": "FULL",
               "cartesian": "CROSS", "leftsemi": "SEMI", "leftanti": "ANTI", "rightsemi": "SEMI",
               "rightanti": "ANTI", "cross": "CROSS"}


def _fill_join(node: PlanNode, op: str, exprs: list) -> None:
    key = op.lower().removesuffix("join")
    node.join_type = _JOIN_TYPES.get(key)
    node.join_algo = "nested_loop" if node.join_type == "CROSS" else "hash"
    conds = [str(e).split(":", 1)[1].strip() for e in exprs
             if str(e).lower().startswith(("joinkey", "joinfilter", "equalityjoincondition"))]
    node.condition = " AND ".join(conds) or None
    node.attrs["has_condition"] = bool(conds)
