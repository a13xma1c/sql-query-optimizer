"""Compare two execution plans (e.g. two versions of a query)."""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .model import Plan, PlanNode

SAME, CHANGED, ADDED, REMOVED = "same", "changed", "added", "removed"


@dataclass
class DiffEntry:
    status: str
    depth: int
    old: PlanNode | None = None
    new: PlanNode | None = None
    changes: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        node = self.new or self.old
        assert node is not None
        return node.title


@dataclass
class PlanDiff:
    entries: list[DiffEntry]
    old_cost: float | None
    new_cost: float | None
    old_time_ms: float | None
    new_time_ms: float | None
    summary: list[str]

    @property
    def new_ids(self) -> dict[int, str]:
        return {e.new.id: e.status for e in self.entries if e.new is not None}

    @property
    def old_ids(self) -> dict[int, str]:
        return {e.old.id: e.status for e in self.entries if e.old is not None}

    @property
    def identical(self) -> bool:
        return all(e.status == SAME for e in self.entries)


def _rel_change(a: float | None, b: float | None, threshold: float = 0.1) -> bool:
    if a is None or b is None:
        return False
    base = max(abs(a), 1.0)
    return abs(b - a) / base > threshold


def _node_changes(old: PlanNode, new: PlanNode) -> list[str]:
    ch: list[str] = []
    if old.join_algo != new.join_algo and (old.join_algo or new.join_algo):
        ch.append(f"join algorithm {old.join_algo} -> {new.join_algo}")
    if old.join_type != new.join_type and (old.join_type or new.join_type):
        ch.append(f"join type {old.join_type} -> {new.join_type}")
    if (old.filter or "") != (new.filter or ""):
        ch.append(f"filter: {old.filter or '-'}  ->  {new.filter or '-'}")
    if (old.condition or "") != (new.condition or ""):
        ch.append(f"condition: {old.condition or '-'}  ->  {new.condition or '-'}")
    if _rel_change(old.est_rows, new.est_rows):
        ch.append(f"estimated rows {_n(old.est_rows)} -> {_n(new.est_rows)}")
    if _rel_change(old.actual_rows, new.actual_rows):
        ch.append(f"actual rows {_n(old.actual_rows)} -> {_n(new.actual_rows)}")
    if _rel_change(old.self_cost, new.self_cost, 0.25):
        ch.append(f"cost {_n(old.self_cost)} -> {_n(new.self_cost)}")
    if _rel_change(old.self_time_ms, new.self_time_ms, 0.25) and max(old.self_time_ms or 0, new.self_time_ms or 0) > 0.5:
        ch.append(f"time {_n(old.self_time_ms)}ms -> {_n(new.self_time_ms)}ms")
    return ch


def _n(v: float | None) -> str:
    if v is None:
        return "?"
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:.2f}".rstrip("0").rstrip(".")


def diff_plans(old: Plan, new: Plan) -> PlanDiff:
    entries: list[DiffEntry] = []
    _align(old.root, new.root, 0, entries)
    summary = _summary(old, new, entries)
    return PlanDiff(entries, old.total_cost, new.total_cost, old.total_time_ms, new.total_time_ms, summary)


def _align(o: PlanNode | None, n: PlanNode | None, depth: int, out: list[DiffEntry]) -> None:
    if o is not None and n is not None:
        if o.signature == n.signature:
            changes = _node_changes(o, n)
            out.append(DiffEntry(CHANGED if changes else SAME, depth, o, n, changes))
        else:
            # different operator at the same position: report as replacement
            out.append(DiffEntry(REMOVED, depth, o, None))
            out.append(DiffEntry(ADDED, depth, None, n))
        _align_children(o.children, n.children, depth + 1, out)
    elif o is not None:
        _emit_subtree(o, depth, out, REMOVED)
    elif n is not None:
        _emit_subtree(n, depth, out, ADDED)


def _align_children(oc: list[PlanNode], nc: list[PlanNode], depth: int, out: list[DiffEntry]) -> None:
    sm = SequenceMatcher(a=[c.signature for c in oc], b=[c.signature for c in nc], autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for o, n in zip(oc[i1:i2], nc[j1:j2], strict=True):
                _align(o, n, depth, out)
        else:
            olds, news = oc[i1:i2], nc[j1:j2]
            paired = min(len(olds), len(news)) if tag == "replace" else 0
            for o, n in zip(olds[:paired], news[:paired], strict=True):
                out.append(DiffEntry(REMOVED, depth, o, None))
                _emit_subtree(o, depth + 1, out, REMOVED, skip_root=True)
                out.append(DiffEntry(ADDED, depth, None, n))
                _emit_subtree(n, depth + 1, out, ADDED, skip_root=True)
            for o in olds[paired:]:
                _emit_subtree(o, depth, out, REMOVED)
            for n in news[paired:]:
                _emit_subtree(n, depth, out, ADDED)


def _emit_subtree(node: PlanNode, depth: int, out: list[DiffEntry], status: str, skip_root: bool = False) -> None:
    if not skip_root:
        out.append(DiffEntry(status, depth, node if status == REMOVED else None, node if status == ADDED else None))
    for c in node.children:
        _emit_subtree(c, depth + 1, out, status)


def _summary(old: Plan, new: Plan, entries: list[DiffEntry]) -> list[str]:
    lines: list[str] = []
    if old.total_cost and new.total_cost:
        pct = (new.total_cost - old.total_cost) / old.total_cost * 100
        lines.append(f"Estimated total cost: {_n(old.total_cost)} -> {_n(new.total_cost)} ({pct:+.0f}%)")
    ot, nt = old.total_time_ms, new.total_time_ms
    if ot and nt:
        lines.append(f"Execution time: {_n(ot)} ms -> {_n(nt)} ms ({(nt - ot) / ot * 100:+.0f}%)")
    lines.append(f"Operators: {len(old.nodes())} -> {len(new.nodes())}")

    def count(plan: Plan, kind: str) -> int:
        return sum(1 for n in plan.nodes() if n.kind == kind)

    for kind, label in (("scan", "scans"), ("join", "joins"), ("index_scan", "index scans"), ("sort", "sorts")):
        a, b = count(old, kind), count(new, kind)
        if a != b:
            lines.append(f"Number of {label}: {a} -> {b}")

    algos = lambda p: sorted(n.join_algo for n in p.nodes() if n.join_algo)  # noqa: E731
    if algos(old) != algos(new):
        lines.append(f"Join algorithms: {', '.join(algos(old)) or '-'} -> {', '.join(algos(new)) or '-'}")

    rels = lambda p: {n.relation for n in p.nodes() if n.relation}  # noqa: E731
    gone, came = rels(old) - rels(new), rels(new) - rels(old)
    if gone:
        lines.append("Tables no longer read: " + ", ".join(sorted(gone)))
    if came:
        lines.append("Tables newly read: " + ", ".join(sorted(came)))
    counts = {s: sum(1 for e in entries if e.status == s) for s in (ADDED, REMOVED, CHANGED)}
    if not any(counts.values()):
        lines.append("Plans are structurally identical.")
    else:
        lines.append(f"Operator diff: {counts[ADDED]} added, {counts[REMOVED]} removed, {counts[CHANGED]} changed")
    return lines
