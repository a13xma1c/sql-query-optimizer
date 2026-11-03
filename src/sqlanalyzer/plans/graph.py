"""Render a :class:`Plan` as a Graphviz graph."""

from __future__ import annotations

import math
import shutil

import graphviz

from .diff import ADDED, CHANGED, REMOVED, PlanDiff
from .model import Plan, PlanNode

_KIND_FILL = {"scan": "#fde2e2", "index_scan": "#dff3e3", "join": "#dbe8fb", "aggregate": "#fff2cc",
              "sort": "#ece0f7", "exchange": "#e5e5e5", "append": "#e5e5e5", "other": "#ffffff"}
_STATUS_BORDER = {ADDED: "#1a9850", REMOVED: "#d73027", CHANGED: "#f59e0b"}
_SEVERITY_BORDER = {"error": "#d73027", "warning": "#f59e0b"}


class GraphvizMissing(RuntimeError):
    pass


def graphviz_available() -> bool:
    return shutil.which("dot") is not None


def _human(n: float | None) -> str:
    if n is None:
        return "?"
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def _esc(text: str, limit: int = 60) -> str:
    text = text.replace("\n", " ")
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(node: PlanNode) -> str:
    rows = [f"<B>{_esc(node.op)}</B>"]
    if node.relation:
        rows.append(_esc(node.relation + (f" ({node.alias})" if node.alias and node.alias != node.relation else "")))
    if node.join_type or node.join_algo:
        rows.append(_esc(" · ".join(x for x in (node.join_type, node.join_algo) if x)))
    if node.condition:
        rows.append(f"<I>on {_esc(node.condition)}</I>")
    if node.filter:
        rows.append(f"<I>filter {_esc(node.filter)}</I>")
    est, act = node.est_rows, node.actual_rows
    metrics = []
    if act is not None:
        metrics.append(f"rows {_human(act)}" + (f" (est {_human(est)})" if est is not None else ""))
    elif est is not None:
        metrics.append(f"est rows {_human(est)}")
    if node.self_time_ms is not None:
        metrics.append(f"{node.self_time_ms:.2f} ms")
    elif node.self_cost is not None:
        metrics.append(f"cost {_human(node.self_cost)}")
    if metrics:
        rows.append(" | ".join(metrics))
    return "<" + "<BR/>".join(rows) + ">"


def build_graph(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
                prefix: str = "n") -> graphviz.Digraph:
    """Build the graph. ``severities`` marks problem nodes; ``status`` colours diff results."""
    severities = severities or {}
    status = status or {}
    g = graphviz.Digraph("plan", graph_attr={"rankdir": "BT", "nodesep": "0.35", "ranksep": "0.5", "bgcolor": "transparent"},
                         node_attr={"shape": "box", "style": "rounded,filled", "fontname": "Helvetica", "fontsize": "10",
                                    "margin": "0.12,0.08"},
                         edge_attr={"fontname": "Helvetica", "fontsize": "9", "color": "#666666"})
    for node in plan.nodes():
        attrs = {"label": _label(node), "fillcolor": _KIND_FILL.get(node.kind, "#ffffff"),
                 "tooltip": node.title}
        border = _STATUS_BORDER.get(status.get(node.id, "")) or _SEVERITY_BORDER.get(severities.get(node.id, ""))
        if border:
            attrs.update(color=border, penwidth="2.5")
        else:
            attrs.update(color="#888888")
        g.node(f"{prefix}{node.id}", **attrs)
        for child in node.children:
            width = 1.0
            rows = child.output_rows
            if rows:
                width = min(1.0 + max(0.0, math.log10(max(rows, 1)) - 2) * 0.6, 6.0)
            g.edge(f"{prefix}{child.id}", f"{prefix}{node.id}", label=_human(rows) if rows is not None else "",
                   penwidth=f"{width:.1f}")
    return g


def render_svg(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None) -> bytes:
    if not graphviz_available():
        raise GraphvizMissing("The Graphviz 'dot' executable was not found. Install Graphviz "
                              "(e.g. 'brew install graphviz' or 'apt install graphviz') to visualize plans.")
    return build_graph(plan, severities, status).pipe(format="svg")


def render_diff_svgs(diff: PlanDiff, old: Plan, new: Plan) -> tuple[bytes, bytes]:
    return render_svg(old, status=diff.old_ids), render_svg(new, status=diff.new_ids)
