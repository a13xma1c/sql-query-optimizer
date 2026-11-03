"""Rule-based detection of expensive operators in an execution plan."""

from __future__ import annotations

import re

from ..dialects import Engine
from ..models import Category, Finding, Severity
from .model import APPEND, INDEX_SCAN, JOIN, SCAN, Plan, PlanNode

LARGE_SCAN_ROWS = 100_000
BIG_JOIN_INPUT = 10_000
MISESTIMATE_FACTOR = 10.0
EXPLOSION_FACTOR = 10.0
HOT_NODE_SHARE = 0.5
POOR_FILTER_RATIO = 0.9
PARTITION_SCAN_RATIO = 0.5


def _fmt(n: float | None) -> str:
    if n is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def analyze_plan(plan: Plan) -> list[Finding]:
    out: list[Finding] = []
    for node in plan.nodes():
        out += _scan_rules(node, plan)
        if node.kind == JOIN:
            out += _join_rules(node)
        out += _spill_rules(node)
        out += _estimate_rules(node)
    out += _hot_node_rules(plan)
    out += _partition_rules(plan)
    return out


def _F(rule: str, sev: Severity, cat: Category, node: PlanNode, msg: str, tip: str = "") -> Finding:
    return Finding(rule, sev, cat, f"[{node.title} #{node.id}] {msg}", tip, node_id=node.id)


# ------------------------------------------------------------------------------ scans
def _scan_rules(node: PlanNode, plan: Plan) -> list[Finding]:
    if node.kind != SCAN:
        return []
    out: list[Finding] = []
    # rows the scan had to read (before filter) vs rows it emitted
    emitted = node.output_rows
    removed = node.attrs.get("Rows Removed by Filter")
    read = (emitted or 0) + (float(removed) if removed is not None else 0)
    if removed is None and node.attrs.get("input_rows") is not None:
        read = float(node.attrs["input_rows"])
    size = read if (removed is not None or node.attrs.get("input_rows") is not None) else (node.est_total_rows or emitted)
    if node.est_rows is not None and node.actual_rows is None and node.filter is not None and plan.engine is Engine.TRINO:
        size = node.est_rows  # Trino estimate is for the scan input

    if size is not None and size >= LARGE_SCAN_ROWS:
        if node.filter is None and not node.attrs.get("dynamicFilters"):
            out.append(_F("large-unfiltered-scan", Severity.WARNING, Category.SCAN, node,
                          f"Full scan of ~{_fmt(size)} rows with no filter.",
                          "Add a selective predicate, project fewer columns, or pre-aggregate."))
        elif removed is not None and read > 0 and float(removed) / read >= POOR_FILTER_RATIO:
            out.append(_F("scan-discards-rows", Severity.WARNING, Category.SCAN, node,
                          f"Scan read ~{_fmt(read)} rows and discarded {float(removed) / read:.0%} in the filter.",
                          "An index (or partition/cluster key) on the filtered column(s) would avoid reading these rows."))
        elif plan.engine is Engine.POSTGRES and node.op == "Seq Scan":
            out.append(_F("large-seq-scan", Severity.INFO, Category.SCAN, node,
                          f"Sequential scan over ~{_fmt(size)} rows.",
                          "Check whether an index on the filter columns exists and the predicate is sargable."))
    if node.filter and re.search(r"\b(cast|::|date_trunc|lower|upper|substr|to_char|year|month|date)\b\s*\(?", node.filter, re.I) \
            and size is not None and size >= LARGE_SCAN_ROWS:
        out.append(_F("function-in-scan-filter", Severity.INFO, Category.SCAN, node,
                      "Scan filter applies a function/cast to a column; this prevents index and min/max pruning.",
                      "Rewrite the predicate to compare the bare column with a constant range."))
    if (plan.engine is Engine.POSTGRES and node.op == "Index Only Scan"
            and float(node.attrs.get("Heap Fetches") or 0) > 1000):
        out.append(_F("index-only-heap-fetches", Severity.INFO, Category.SCAN, node,
                      f"Index Only Scan performed {node.attrs['Heap Fetches']} heap fetches.",
                      "VACUUM the table so the visibility map allows true index-only access."))
    return out


# ------------------------------------------------------------------------------ joins
def _join_rules(node: PlanNode) -> list[Finding]:
    out: list[Finding] = []
    kids = node.children
    sizes = [k.output_rows for k in kids]
    left = sizes[0] if sizes else None
    right = sizes[1] if len(sizes) > 1 else None
    known = [s for s in sizes if s is not None]
    biggest = max(known) if known else None

    if node.join_type == "CROSS" or (node.join_algo == "nested_loop" and not node.attrs.get("has_condition", True)
                                     and node.condition is None):
        sev = Severity.ERROR if (biggest or 0) >= BIG_JOIN_INPUT else Severity.WARNING
        out.append(_F("cartesian-product", sev, Category.JOIN, node,
                      "Join without a join condition (Cartesian product)"
                      + (f"; inputs {_fmt(left)} x {_fmt(right)} rows." if left is not None and right is not None else "."),
                      "Add a join predicate between the two relations."))
    elif node.join_algo == "nested_loop" and left is not None and right is not None:
        # Nested loop: cost ~ outer * inner executions unless the inner uses an index.
        inner = kids[1]
        inner_indexed = any(n.kind == INDEX_SCAN for n in inner.walk())
        if left * right >= 1e7 and not inner_indexed:
            out.append(_F("expensive-nested-loop", Severity.WARNING, Category.JOIN, node,
                          f"Nested loop join over {_fmt(left)} x {_fmt(right)} rows without an index on the inner side.",
                          "Add an index on the inner join key, or make the join eligible for a hash/merge join (equality predicate, matching types)."))

    # row explosion
    out_rows = node.output_rows
    if out_rows is not None and biggest and node.join_type not in ("SEMI", "ANTI", "CROSS") \
            and out_rows > EXPLOSION_FACTOR * biggest and out_rows >= BIG_JOIN_INPUT:
        out.append(_F("join-row-explosion", Severity.WARNING, Category.JOIN, node,
                      f"Join emits ~{_fmt(out_rows)} rows from inputs of at most {_fmt(biggest)} (many-to-many join?).",
                      "Check that join keys are unique on one side; deduplicate or aggregate before joining."))

    return out


# ------------------------------------------------------------------------------ spills
def _spill_rules(node: PlanNode) -> list[Finding]:
    out: list[Finding] = []
    if str(node.attrs.get("Sort Space Type", "")).lower() == "disk":
        out.append(_F("sort-spill", Severity.WARNING, Category.PLAN, node,
                      f"Sort spilled to disk ({node.attrs.get('Sort Space Used')} kB).",
                      "Increase work_mem, reduce the sorted row width, or add an index that provides the ordering."))
    batches = node.attrs.get("Hash Batches")
    if batches is not None and float(batches) > 1:
        out.append(_F("hash-spill", Severity.WARNING, Category.PLAN, node,
                      f"Hash used {batches} batches (spilled to disk).",
                      "Increase work_mem or reduce the build side (filter/aggregate earlier)."))
    return out


# --------------------------------------------------------------------------- estimates
def _estimate_rules(node: PlanNode) -> list[Finding]:
    if node.actual_rows is None or node.est_total_rows is None:
        return []
    est, act = max(node.est_total_rows, 1.0), max(node.actual_rows, 1.0)
    ratio = max(est / act, act / est)
    if ratio >= MISESTIMATE_FACTOR and max(est, act) >= 1000:
        kind = "under" if act > est else "over"
        return [_F("row-misestimate", Severity.INFO, Category.PLAN, node,
                   f"Optimizer {kind}-estimated rows by {ratio:.0f}x (estimated {_fmt(est)}, actual {_fmt(act)}).",
                   "Refresh statistics (ANALYZE) or add extended statistics; bad estimates lead to wrong join order/strategy.")]
    return []


def _hot_node_rules(plan: Plan) -> list[Finding]:
    nodes = plan.nodes()
    if len(nodes) < 3:
        return []
    out: list[Finding] = []
    total = sum(n.self_time_ms or 0.0 for n in nodes)
    if total > 0:
        for n in nodes:
            if (n.self_time_ms or 0) / total >= HOT_NODE_SHARE and total >= 1.0:
                out.append(_F("hot-operator", Severity.INFO, Category.PLAN, n,
                              f"Accounts for {(n.self_time_ms or 0) / total:.0%} of measured execution time.",
                              "Focus tuning on this operator first."))
        return out
    costs = [(n, n.self_cost) for n in nodes if n.self_cost is not None]
    total_c = sum(c for _, c in costs)
    if total_c > 0:
        for n, c in costs:
            if c / total_c >= HOT_NODE_SHARE and total_c >= 1000:
                out.append(_F("hot-operator", Severity.INFO, Category.PLAN, n,
                              f"Accounts for {c / total_c:.0%} of the estimated plan cost.",
                              "Focus tuning on this operator first."))
    return out


# -------------------------------------------------------------------- partition pruning
def _partition_rules(plan: Plan) -> list[Finding]:
    out: list[Finding] = []
    for node in plan.nodes():
        if plan.engine is Engine.SNOWFLAKE and node.kind == SCAN:
            total = _num(node.attrs.get("partitionsTotal"))
            assigned = _num(node.attrs.get("partitionsAssigned"))
            if total and assigned is not None and total >= 10 and assigned / total >= PARTITION_SCAN_RATIO:
                sev = Severity.WARNING if assigned == total else Severity.INFO
                out.append(_F("poor-partition-pruning", sev, Category.PARTITION, node,
                              f"Scans {int(assigned)} of {int(total)} micro-partitions ({assigned / total:.0%}).",
                              "Filter on the clustering key with a bare-column predicate, or define/recluster a clustering key."))
        elif plan.engine is Engine.TRINO and node.kind == SCAN:
            keys = node.attrs.get("partition_keys") or []
            constrained = set(node.attrs.get("constrained_partition_keys") or [])
            unconstrained = [k for k in keys if k not in constrained]
            if keys and unconstrained and not node.attrs.get("dynamicFilters"):
                out.append(_F("no-partition-pruning", Severity.WARNING, Category.PARTITION, node,
                              f"Partition column(s) {', '.join(unconstrained)} have no constraint; all partitions are read.",
                              "Filter on the partition column(s) with literal comparisons (not casts/functions)."))
            elif keys and node.filter and re.search(r"cast\(|\(\w+\(", node.filter, re.I):
                out.append(_F("partition-filter-not-pushed", Severity.INFO, Category.PARTITION, node,
                              "Filter is evaluated after reading; it may not be pushed into partition pruning.",
                              "Compare the bare partition column with literals."))
        elif plan.engine is Engine.POSTGRES and node.kind == APPEND:
            removed = node.attrs.get("Subplans Removed")
            scanned = len(node.children)
            if removed is not None:
                if float(removed) == 0 and scanned >= 8:
                    out.append(_F("no-partition-pruning", Severity.WARNING, Category.PARTITION, node,
                                  f"All {scanned} partitions are scanned (no plan-time pruning).",
                                  "Filter on the partition key using constants; avoid wrapping it in functions."))
            elif scanned >= 8 and all(c.kind in (SCAN, INDEX_SCAN) for c in node.children) and _same_parent_table(node):
                out.append(_F("no-partition-pruning", Severity.WARNING, Category.PARTITION, node,
                              f"Append over {scanned} partitions of one table: pruning removed none.",
                              "Filter on the partition key using constants; avoid wrapping it in functions."))
    return out


def _same_parent_table(append: PlanNode) -> bool:
    names = [re.sub(r"[_\d]+$", "", (c.relation or "").split(".")[-1]) for c in append.children]
    return len(set(names)) == 1 and names[0] != ""


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
