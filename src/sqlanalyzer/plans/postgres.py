"""PostgreSQL ``EXPLAIN (FORMAT JSON)`` parser."""

from __future__ import annotations

import json

from ..dialects import Engine
from .model import (
    AGG,
    APPEND,
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

_JOIN_ALGO = {"Nested Loop": "nested_loop", "Hash Join": "hash", "Merge Join": "merge"}
_KEEP = (
    "Rows Removed by Filter", "Rows Removed by Index Recheck", "Heap Fetches", "Sort Method",
    "Sort Space Type", "Sort Space Used", "Sort Key", "Hash Batches", "Original Hash Batches",
    "Peak Memory Usage", "Subplans Removed", "Workers Planned", "Workers Launched", "Group Key",
    "Strategy", "Parent Relationship", "Index Name", "Scan Direction", "Recheck Cond",
    "Shared Hit Blocks", "Shared Read Blocks", "Join Filter", "Plan Width", "Parallel Aware",
)


def _kind(node_type: str) -> str:
    if node_type in _JOIN_ALGO:
        return JOIN
    if "Index" in node_type and "Scan" in node_type and "Bitmap Heap" not in node_type:
        return INDEX_SCAN
    if node_type == "Bitmap Heap Scan":
        return INDEX_SCAN
    if node_type.endswith("Scan") and node_type not in ("Result",):
        return SCAN
    if "Aggregate" in node_type:
        return AGG
    if node_type in ("Sort", "Incremental Sort"):
        return SORT
    if node_type in ("Gather", "Gather Merge"):
        return EXCHANGE
    if node_type in ("Append", "Merge Append"):
        return APPEND
    return OTHER


def parse_postgres(raw: str | list | dict) -> Plan:
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or "Plan" not in data:
        raise PlanParseError("Not a PostgreSQL EXPLAIN (FORMAT JSON) document.")
    ids = IdCounter()
    root = _node(data["Plan"], ids)
    analyzed = "Execution Time" in data or root.actual_rows is not None
    stats = {k: data[k] for k in ("Planning Time", "Execution Time") if k in data}
    return Plan(Engine.POSTGRES, root, raw if isinstance(raw, str) else json.dumps(data), analyzed, stats)


def _node(d: dict, ids: IdCounter) -> PlanNode:
    nt = d.get("Node Type", "?")
    loops = d.get("Actual Loops")
    actual = d.get("Actual Rows")
    time_ms = d.get("Actual Total Time")
    node = PlanNode(
        id=ids.next(),
        op=nt,
        kind=_kind(nt),
        relation=_qualify(d.get("Schema"), d.get("Relation Name")) or d.get("CTE Name"),
        alias=d.get("Alias"),
        join_type=(d.get("Join Type") or "").upper() or None,
        join_algo=_JOIN_ALGO.get(nt),
        est_rows=to_float(d.get("Plan Rows")),
        actual_rows=(to_float(actual) * (to_float(loops) or 1)) if actual is not None else None,
        cost=to_float(d.get("Total Cost")),
        time_ms=(to_float(time_ms) * (to_float(loops) or 1)) if time_ms is not None else None,
        filter=d.get("Filter") or d.get("Join Filter"),
        condition=d.get("Hash Cond") or d.get("Merge Cond") or d.get("Index Cond") or d.get("Recheck Cond"),
    )
    node.attrs["loops"] = loops if loops is not None else 1
    node.attrs.update({k: d[k] for k in _KEEP if k in d})
    if d.get("Node Type") == "Hash Join" or nt.endswith("Join"):
        node.attrs.setdefault("has_condition", node.condition is not None)
    if nt == "Nested Loop":
        node.attrs["has_condition"] = bool(d.get("Join Filter")) or any(
            "Index Cond" in c for c in _walk_dicts(d.get("Plans", [])))
    node.children = [_node(c, ids) for c in d.get("Plans", [])]
    if node.time_ms is not None:
        node.self_time_ms = max(node.time_ms - sum(c.time_ms or 0.0 for c in node.children), 0.0)
    return node


def _walk_dicts(plans: list[dict]):
    for p in plans:
        yield p
        yield from _walk_dicts(p.get("Plans", []))


def _qualify(schema: str | None, rel: str | None) -> str | None:
    if not rel:
        return None
    return f"{schema}.{rel}" if schema else rel
