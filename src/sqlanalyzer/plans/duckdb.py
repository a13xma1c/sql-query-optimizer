"""DuckDB ``EXPLAIN (FORMAT JSON)`` / ``EXPLAIN ANALYZE`` parser."""

from __future__ import annotations

import json

from ..dialects import Engine
from .model import (
    AGG,
    EXCHANGE,
    INDEX_SCAN,
    JOIN,
    OTHER,
    SCAN,
    SORT,
    IdCounter,
    Plan,
    PlanNode,
    PlanParseError,
    to_float,
)


def _kind(name: str) -> str:
    n = name.upper()
    if "JOIN" in n or n in ("CROSS_PRODUCT", "ASOF_JOIN", "POSITIONAL_JOIN"):
        return JOIN
    if n == "INDEX_SCAN":
        return INDEX_SCAN
    if n in ("SEQ_SCAN", "TABLE_SCAN", "READ_PARQUET", "READ_CSV", "READ_JSON", "PARQUET_SCAN") or n.endswith("_SCAN"):
        return SCAN
    if "AGGREGATE" in n or n == "PERFECT_HASH_GROUP_BY":
        return AGG
    if n in ("ORDER_BY", "TOP_N"):
        return SORT
    if "EXCHANGE" in n:
        return EXCHANGE
    return OTHER


def _algo(name: str, extra: dict) -> str | None:
    n = name.upper()
    if n == "HASH_JOIN":
        return "hash"
    if n == "NESTED_LOOP_JOIN":
        return "nested_loop"
    if n == "MERGE_JOIN":
        return "merge"
    if n in ("CROSS_PRODUCT", "BLOCKWISE_NL_JOIN"):
        return "nested_loop"
    if n == "PIECEWISE_MERGE_JOIN":
        return "merge"
    return None


def parse_duckdb(raw: str | list | dict) -> Plan:
    data = json.loads(raw) if isinstance(raw, str) else raw
    analyzed = isinstance(data, dict) and ("operator" in data or "children" in data or "latency" in data)
    if isinstance(data, dict):
        top = data.get("children") or data.get("operator") or []
        roots = top if isinstance(top, list) else [top]
        stats = {k: data[k] for k in ("latency", "total_bytes_read", "cpu_time", "rows_returned") if k in data}
    elif isinstance(data, list):
        roots, stats = data, {}
    else:
        raise PlanParseError("Not a DuckDB EXPLAIN (FORMAT JSON) document.")
    if not roots:
        raise PlanParseError("DuckDB plan is empty.")
    ids = IdCounter()
    nodes = [_node(r, ids) for r in roots]
    root = _strip_wrappers(nodes[0]) if len(nodes) == 1 else PlanNode(ids.next(), "PLAN", children=nodes)
    return Plan(Engine.DUCKDB, root, raw if isinstance(raw, str) else json.dumps(data), analyzed, stats)


_WRAPPERS = {"RESULT_COLLECTOR", "EXPLAIN_ANALYZE", "QUERY"}


def _strip_wrappers(node: PlanNode) -> PlanNode:
    while node.op.upper() in _WRAPPERS and len(node.children) == 1:
        node = node.children[0]
    return node


def _node(d: dict, ids: IdCounter) -> PlanNode:
    name = str(d.get("name") or d.get("operator_type") or d.get("type") or "?")
    extra = d.get("extra_info") or {}
    if isinstance(extra, str):
        extra = _parse_extra_str(extra)
    extra = {str(k): ("\n".join(v) if isinstance(v, list) else str(v)) for k, v in extra.items()}

    node = PlanNode(id=ids.next(), op=name, kind=_kind(name))
    node.join_algo = _algo(name, extra)
    if node.kind == JOIN:
        node.join_type = (extra.get("Join Type") or "").upper() or None
        node.condition = extra.get("Conditions")
        if name.upper() == "CROSS_PRODUCT":
            node.join_type = "CROSS"
    node.est_rows = to_float(extra.get("Estimated Cardinality"))
    node.relation = extra.get("Table") or extra.get("Function") or None
    if node.kind != JOIN:
        node.filter = extra.get("Filters") or extra.get("Expression")
    rows = d.get("operator_cardinality", d.get("intermediate_rows", d.get("cardinality")))
    node.actual_rows = to_float(rows)
    timing = d.get("operator_timing", d.get("timing", d.get("cpu_time")))
    if timing is not None:
        node.self_time_ms = float(timing) * 1000.0
    node.attrs.update({k: v for k, v in extra.items()
                       if k not in ("Estimated Cardinality",)})
    node.children = [_node(c, ids) for c in d.get("children", [])]
    if node.self_time_ms is not None:
        node.time_ms = node.self_time_ms + sum(c.time_ms or 0.0 for c in node.children)
    return node


def _parse_extra_str(text: str) -> dict:
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
        elif line.strip():
            out.setdefault("Info", line.strip())
    return out
