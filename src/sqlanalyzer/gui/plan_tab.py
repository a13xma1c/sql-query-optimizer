"""Execution plan visualization: Graphviz graph + operator tree."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import Finding, Severity
from ..plans import Plan, PlanNode
from ..plans.explain import PlanExplanation, explain_plan
from ..plans.graph import GraphvizMissing, heat_map, render_pdf, render_png, render_svg
from .findings_tab import SEVERITY_COLORS
from .svg_view import SvgView

HEADERS = ["Operator", "Est. rows", "Actual rows", "Cost", "Time (ms)", "Issues"]


def severity_by_node(findings: list[Finding]) -> dict[int, str]:
    worst: dict[int, Severity] = {}
    for f in findings:
        if f.node_id is not None and (f.node_id not in worst or f.severity.rank < worst[f.node_id].rank):
            worst[f.node_id] = f.severity
    return {k: v.value for k, v in worst.items()}


def show_plan_graph(view: SvgView, plan: Plan | None, severities: dict[int, str] | None = None,
                    status: dict[int, str] | None = None, heat: dict[int, float] | None = None,
                    collapsed: set[int] | None = None) -> None:
    if plan is None:
        view.show_message("No plan loaded.")
        return
    try:
        svg = render_svg(plan, severities, status, heat=heat, collapsed=collapsed)
        view.load_svg(svg, hit_map={f"n{n.id}": n.id for n in plan.nodes()})
    except GraphvizMissing as exc:
        view.show_message(str(exc))
    except Exception as exc:  # noqa: BLE001 - e.g. dot crash on malformed label
        view.show_message(f"Graphviz failed: {exc}")


def fmt(v: float | None, digits: int = 0) -> str:
    if v is None:
        return ""
    return f"{v:,.{digits}f}"


class PlanTab(QWidget):
    explain_requested = Signal(bool)  # analyze?
    paste_requested = Signal()
    plan_loaded = Signal(str)  # raw plan text from a file

    def __init__(self) -> None:
        super().__init__()
        self._plan: Plan | None = None
        self._findings: list[Finding] = []
        self._explanation: PlanExplanation | None = None
        self._items: dict[int, QTreeWidgetItem] = {}
        self._collapsed: set[int] = set()

        self.analyze_box = QCheckBox("ANALYZE (executes the query)")
        explain = QPushButton("Explain")
        explain.setToolTip("Run EXPLAIN on the connected database")
        explain.clicked.connect(lambda: self.explain_requested.emit(self.analyze_box.isChecked()))
        paste = QPushButton("Paste plan…")
        paste.clicked.connect(self.paste_requested)
        load = QPushButton("Open plan file…")
        load.clicked.connect(self._open_file)
        self.heat_box = QCheckBox("Heat-map")
        self.heat_box.setToolTip("Colour operators by their share of total cost/time (darker = hotter). "
                                 "Labels also show the percentage so the view does not rely on colour alone.")
        self.heat_box.toggled.connect(self._refresh_graph)
        expand_all = QPushButton("Expand all")
        expand_all.clicked.connect(self._expand_all)
        self.export_btn = QToolButton()
        self.export_btn.setText("Export…")
        self.export_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        export_menu = QMenu(self.export_btn)
        export_menu.addAction("SVG…", lambda: self._export("svg"))
        export_menu.addAction("PNG…", lambda: self._export("png"))
        export_menu.addAction("PDF…", lambda: self._export("pdf"))
        self.export_btn.setMenu(export_menu)
        fit = QPushButton("Fit")
        zin, zout = QPushButton("+"), QPushButton("−")
        for b in (zin, zout, fit):
            b.setFixedWidth(40)

        self.view = SvgView()
        self.view.show_message("Run Explain, or paste an existing plan, to visualize it.")
        self.view.elementClicked.connect(self.select_node)
        self.view.elementDoubleClicked.connect(self._toggle_collapse)
        fit.clicked.connect(self.view.fit)
        zin.clicked.connect(lambda: self.view.zoom(1.25))
        zout.clicked.connect(lambda: self.view.zoom(0.8))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.tree.setColumnWidth(0, 240)
        for i in range(1, len(HEADERS)):
            self.tree.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._show_detail)
        self.detail = QTextBrowser()
        self.explanation_view = QTextBrowser()
        self.explanation_view.setOpenLinks(False)
        self.explanation_view.anchorClicked.connect(self._explanation_clicked)
        self.panes = QTabWidget()
        self.panes.addTab(self.detail, "Operator details")
        self.panes.addTab(self.explanation_view, "Explanation")

        self.source_label = QLabel("")
        self.source_label.setWordWrap(True)
        self.source_label.setStyleSheet("color: #57606a;")

        bottom = QSplitter(Qt.Orientation.Vertical)
        bottom.addWidget(self.tree)
        bottom.addWidget(self.panes)
        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 4)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self.view)
        main.addWidget(bottom)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)

        bar = QHBoxLayout()
        left: tuple[QWidget, ...] = (explain, self.analyze_box, paste, load)
        right: tuple[QWidget, ...] = (self.heat_box, expand_all, zout, zin, fit, self.export_btn)
        for w in left:
            bar.addWidget(w)
        bar.addStretch(1)
        for w in right:
            bar.addWidget(w)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.source_label)
        layout.addWidget(main, 1)

    # ------------------------------------------------------------------------ data
    def set_plan(self, plan: Plan | None, findings: list[Finding],
                 explanation: PlanExplanation | None = None) -> None:
        """Show ``plan`` (an engine plan or the offline one) and its explanation."""
        self._plan = plan
        self._findings = findings
        self._explanation = explanation
        self._collapsed = set()
        self.tree.clear()
        self._items.clear()
        self.detail.clear()
        self._refresh_graph()
        self._render_explanation()
        if plan is None:
            self.source_label.setText("")
            return
        # measured columns only make sense for engine plans
        self.tree.setColumnHidden(HEADERS.index("Actual rows"), plan.logical)
        self.tree.setColumnHidden(HEADERS.index("Time (ms)"), plan.logical or not plan.analyzed)
        if plan.logical:
            self.source_label.setText(
                "<b>Offline plan</b> - derived from the SQL, no database was contacted. Operators and join "
                "strategies are what an optimizer typically picks; rows and costs come from the offline cost "
                "model (declare row counts in the Schema tab to tighten them).")
        else:
            self.source_label.setText(f"<b>{plan.source}</b> - loaded from the engine's EXPLAIN output.")

        sev = severity_by_node(findings)
        counts: dict[int, int] = {}
        for f in findings:
            if f.node_id is not None:
                counts[f.node_id] = counts.get(f.node_id, 0) + 1

        def add(node: PlanNode, parent: QTreeWidgetItem | None) -> None:
            label = node.title + (f"  [{node.join_algo}]" if node.join_algo else "")
            item = QTreeWidgetItem([
                label, fmt(node.est_rows), fmt(node.actual_rows),
                fmt(node.cost, 1), fmt(node.self_time_ms, 3), str(counts.get(node.id, "")) if node.id in counts else "",
            ])
            for col in range(1, 5):
                item.setTextAlignment(col, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if node.id in sev:
                item.setForeground(0, QBrush(QColor(SEVERITY_COLORS[Severity(sev[node.id])])))
            item.setData(0, Qt.ItemDataRole.UserRole, node.id)
            (parent.addChild(item) if parent else self.tree.addTopLevelItem(item))
            self._items[node.id] = item
            for child in node.children:
                add(child, item)

        add(plan.root, None)
        self.tree.expandAll()

    def select_node(self, node_id: int) -> None:
        item = self._items.get(node_id)
        if item is not None:
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)

    def _refresh_graph(self) -> None:
        sev = severity_by_node(self._findings)
        heat = heat_map(self._plan) if self.heat_box.isChecked() and self._plan is not None else None
        show_plan_graph(self.view, self._plan, sev, heat=heat, collapsed=self._collapsed)

    def _toggle_collapse(self, node_id: int) -> None:
        """Double-clicking a node with children folds/unfolds its subtree in the graph."""
        if self._plan is None:
            return
        node = self._plan.by_id().get(node_id)
        if node is None or not node.children:
            return
        if node_id in self._collapsed:
            self._collapsed.discard(node_id)
        else:
            self._collapsed.add(node_id)
        self._refresh_graph()

    def _expand_all(self) -> None:
        self._collapsed = set()
        self._refresh_graph()

    # ----------------------------------------------------------------- explanation
    def _render_explanation(self) -> None:
        explanation = self._explanation
        if explanation is None and self._plan is not None:
            explanation = self._explanation = explain_plan(self._plan, self._findings)
        if explanation is None:
            self.explanation_view.setHtml("<p style='color:gray'>The explanation appears once the SQL parses.</p>")
            return
        html = [f"<p>{_h(explanation.headline)}</p><ol>"]
        for step in explanation.steps:
            html.append(f"<li><a href='node:{step.node_id}'><b>{_h(step.title)}</b></a><br>{_h(step.detail)}")
            if step.metrics:
                html.append(f"<br><span style='color:#57606a'>{_h(step.metrics)}</span>")
            for note in step.notes:
                html.append(f"<br><span style='color:#bf8700'>! {_h(note)}</span>")
            html.append("</li>")
        html.append("</ol>")
        if explanation.summary:
            html.append("<p><b>Summary</b></p><ul>")
            html += [f"<li>{_h(s)}</li>" for s in explanation.summary]
            html.append("</ul>")
        self.explanation_view.setHtml("".join(html))

    def _explanation_clicked(self, url) -> None:
        text = url.toString()
        if text.startswith("node:"):
            self.select_node(int(text.split(":", 1)[1]))
            self.panes.setCurrentWidget(self.detail)

    def show_explanation(self) -> None:
        self.panes.setCurrentWidget(self.explanation_view)

    def _show_detail(self) -> None:
        items = self.tree.selectedItems()
        if not items or self._plan is None:
            return
        node = self._plan.by_id().get(items[0].data(0, Qt.ItemDataRole.UserRole))
        if node is None:
            return
        rows = [("Operator", node.op), ("Relation", node.relation), ("Join", " ".join(
                    x for x in (node.join_type, node.join_algo) if x) or None),
                ("Condition", node.condition), ("Filter", node.filter),
                ("Estimated rows", fmt(node.est_rows)), ("Actual rows", fmt(node.actual_rows)),
                ("Self time (ms)", fmt(node.self_time_ms, 3)), ("Total cost", fmt(node.cost, 2))]
        rows += [(k, str(v)) for k, v in node.attrs.items() if not k.startswith("_") and v not in ("", None)]
        body = "".join(f"<tr><td><b>{_h(k)}</b></td><td>{_h(str(v))}</td></tr>" for k, v in rows if v)
        issues = "".join(f"<li>{_h(f.message)}<br><i>{_h(f.suggestion)}</i></li>"
                         for f in self._findings if f.node_id == node.id)
        self.detail.setHtml(f"<table cellspacing=4>{body}</table>"
                            + (f"<h4>Findings</h4><ul>{issues}</ul>" if issues else ""))

    # ----------------------------------------------------------------------- files
    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open plan", "", "Plans (*.json *.txt *.plan);;All files (*)")
        if path:
            with open(path, encoding="utf-8") as fh:
                self.plan_loaded.emit(fh.read())

    def _export(self, fmt: str) -> None:
        if self._plan is None:
            return
        filters = {"svg": "SVG (*.svg)", "png": "PNG (*.png)", "pdf": "PDF (*.pdf)"}
        path, _ = QFileDialog.getSaveFileName(self, "Export plan", f"plan.{fmt}", filters[fmt])
        if not path:
            return
        sev = severity_by_node(self._findings)
        heat = heat_map(self._plan) if self.heat_box.isChecked() else None
        renderer = {"svg": render_svg, "png": render_png, "pdf": render_pdf}[fmt]
        try:
            data = renderer(self._plan, sev, heat=heat, collapsed=self._collapsed)
        except GraphvizMissing as exc:
            self.detail.setHtml(f"<p style='color:#d73027'>{_h(str(exc))}</p>")
            return
        with open(path, "wb") as fh:
            fh.write(data)


def _h(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
