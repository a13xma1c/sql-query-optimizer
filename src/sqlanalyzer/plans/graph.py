"""Render a :class:`Plan` as a Graphviz graph.

Every node and edge gets an explicit Graphviz ``id=`` attribute so the ``<g id="...">``
element in the rendered SVG can be matched back to a plan node id by the GUI (see
``gui/svg_view.py``), enabling click-to-select and collapse/expand. Node labels also
carry a severity/diff *glyph*, not just a colour, so the graph remains legible without
colour (accessibility: colour is never the only carrier of meaning here).
"""

from __future__ import annotations

import math
import shutil

import graphviz

from .diff import ADDED, CHANGED, REMOVED, PlanDiff
from .model import Plan, PlanNode

_KIND_FILL = {"scan": "#fde2e2", "index_scan": "#dff3e3", "join": "#dbe8fb", "aggregate": "#fff2cc",
              "sort": "#ece0f7", "exchange": "#e5e5e5", "append": "#e5e5e5", "other": "#ffffff"}
_STATUS_BORDER = {ADDED: "#1a9850", REMOVED: "#d73027", CHANGED: "#f59e0b"}
_STATUS_GLYPH = {ADDED: "+", REMOVED: "\u2212", CHANGED: "~"}
_SEVERITY_BORDER = {"error": "#d73027", "warning": "#f59e0b"}
_SEVERITY_GLYPH = {"error": "\u26d4", "warning": "\u26a0"}
NODE_PREFIX = "n"
EDGE_PREFIX = "e"
COLLAPSED_PREFIX = "c"

# Heat-map colour ramp (low -> high); a yellow-orange-red sequence that also reads fine
# in grayscale because it is monotonically increasing in darkness, not just hue.
_HEAT_STOPS = [(0.0, (255, 255, 204)), (0.4, (254, 178, 76)), (0.7, (253, 141, 60)), (1.0, (189, 0, 38))]


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


def heat_color(value: float) -> str:
    """Linearly interpolate the heat-map ramp at ``value`` (clamped to [0, 1])."""
    value = max(0.0, min(1.0, value))
    for (lo_t, lo_c), (hi_t, hi_c) in zip(_HEAT_STOPS, _HEAT_STOPS[1:], strict=False):
        if lo_t <= value <= hi_t:
            span = (hi_t - lo_t) or 1.0
            frac = (value - lo_t) / span
            rgb = tuple(round(lo_c[i] + (hi_c[i] - lo_c[i]) * frac) for i in range(3))
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return "#{:02x}{:02x}{:02x}".format(*_HEAT_STOPS[-1][1])


def _glyph_prefix(node: PlanNode, severities: dict[int, str], status: dict[int, str]) -> str:
    glyph = _STATUS_GLYPH.get(status.get(node.id, "")) or _SEVERITY_GLYPH.get(severities.get(node.id, ""))
    return f"{glyph} " if glyph else ""


def _label(node: PlanNode, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
          heat: float | None = None) -> str:
    prefix = _glyph_prefix(node, severities or {}, status or {})
    rows = [f"<B>{_esc(prefix + node.op)}</B>"]
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
    if heat is not None:
        rows.append(f"<FONT COLOR=\"#444444\">heat {heat * 100:.0f}%</FONT>")
    return "<" + "<BR/>".join(rows) + ">"


def _collapsed_label(node: PlanNode, count: int) -> str:
    return f"<<B>{_esc(node.op)}</B><BR/><I>{count} operator(s) collapsed - double-click to expand</I>>"


def build_graph(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
                prefix: str = NODE_PREFIX, heat: dict[int, float] | None = None,
                collapsed: set[int] | None = None) -> graphviz.Digraph:
    """Build the graph. ``severities`` marks problem nodes; ``status`` colours diff results.

    ``heat`` maps node id -> a 0..1 share of the total cost/time, rendered as a colour ramp
    fill instead of the operator-kind colour (a heat-map view). ``collapsed`` is a set of node
    ids whose subtree should be rendered as a single placeholder box (their descendants are not
    emitted at all), used to keep very large plans readable.
    """
    severities = severities or {}
    status = status or {}
    heat = heat or {}
    collapsed = collapsed or set()
    g = graphviz.Digraph("plan", graph_attr={"rankdir": "BT", "nodesep": "0.35", "ranksep": "0.5", "bgcolor": "transparent"},
                         node_attr={"shape": "box", "style": "rounded,filled", "fontname": "Helvetica", "fontsize": "10",
                                    "margin": "0.12,0.08"},
                         edge_attr={"fontname": "Helvetica", "fontsize": "9", "color": "#666666"})

    def emit(node: PlanNode) -> None:
        node_id = f"{prefix}{node.id}"
        if node.id in collapsed and node.children:
            descendants = sum(1 for _ in node.walk()) - 1
            g.node(node_id, label=_collapsed_label(node, descendants), fillcolor="#eeeeee", color="#888888",
                   style="rounded,filled,dashed", id=node_id, tooltip=f"{node.title} (collapsed, {descendants} below)")
            return
        attrs = {"label": _label(node, severities, status, heat.get(node.id)),
                 "fillcolor": heat_color(heat[node.id]) if node.id in heat else _KIND_FILL.get(node.kind, "#ffffff"),
                 "tooltip": node.title, "id": node_id}
        border = _STATUS_BORDER.get(status.get(node.id, "")) or _SEVERITY_BORDER.get(severities.get(node.id, ""))
        if border:
            attrs.update(color=border, penwidth="2.5")
        else:
            attrs.update(color="#888888")
        g.node(node_id, **attrs)
        for child in node.children:
            width = 1.0
            rows = child.output_rows
            if rows:
                width = min(1.0 + max(0.0, math.log10(max(rows, 1)) - 2) * 0.6, 6.0)
            edge_id = f"{EDGE_PREFIX}{child.id}_{node.id}"
            g.edge(f"{prefix}{child.id}", node_id, label=_human(rows) if rows is not None else "",
                   penwidth=f"{width:.1f}", id=edge_id)
            emit(child)

    emit(plan.root)
    return g


def render_svg(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
               heat: dict[int, float] | None = None, collapsed: set[int] | None = None) -> bytes:
    if not graphviz_available():
        raise GraphvizMissing("The Graphviz 'dot' executable was not found. Install Graphviz "
                              "(e.g. 'brew install graphviz' or 'apt install graphviz') to visualize plans.")
    return build_graph(plan, severities, status, heat=heat, collapsed=collapsed).pipe(format="svg")


def render_png(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
               heat: dict[int, float] | None = None, collapsed: set[int] | None = None) -> bytes:
    if not graphviz_available():
        raise GraphvizMissing("The Graphviz 'dot' executable was not found. Install Graphviz "
                              "(e.g. 'brew install graphviz' or 'apt install graphviz') to visualize plans.")
    return build_graph(plan, severities, status, heat=heat, collapsed=collapsed).pipe(format="png")


def render_pdf(plan: Plan, severities: dict[int, str] | None = None, status: dict[int, str] | None = None,
               heat: dict[int, float] | None = None, collapsed: set[int] | None = None) -> bytes:
    if not graphviz_available():
        raise GraphvizMissing("The Graphviz 'dot' executable was not found. Install Graphviz "
                              "(e.g. 'brew install graphviz' or 'apt install graphviz') to visualize plans.")
    return build_graph(plan, severities, status, heat=heat, collapsed=collapsed).pipe(format="pdf")


def heat_map(plan: Plan) -> dict[int, float]:
    """Share (0..1) of the plan's total cost/time each node's *own* (exclusive) cost represents.

    Prefers measured self-time (EXPLAIN ANALYZE); falls back to the cost estimate. Returns an
    empty mapping when neither is available anywhere in the plan (so the caller can tell there is
    nothing to colour).
    """
    nodes = plan.nodes()
    times = {n.id: n.self_time_ms for n in nodes if n.self_time_ms is not None}
    values = times if times else {n.id: n.self_cost for n in nodes if n.self_cost is not None}
    total = sum(values.values())
    if not values or total <= 0:
        return {}
    return {node_id: v / total for node_id, v in values.items()}


def render_diff_svgs(diff: PlanDiff, old: Plan, new: Plan) -> tuple[bytes, bytes]:
    return render_svg(old, status=diff.old_ids), render_svg(new, status=diff.new_ids)
