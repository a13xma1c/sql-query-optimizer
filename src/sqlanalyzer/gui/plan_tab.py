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
    QPushButton,
    QSplitter,
    QTextBrowser,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models import Finding, Severity
from ..plans import Plan, PlanNode
from ..plans.graph import GraphvizMissing, render_svg
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
                    status: dict[int, str] | None = None) -> None:
    if plan is None:
        view.show_message("No plan loaded.")
        return
    try:
        view.load_svg(render_svg(plan, severities, status))
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
        self._items: dict[int, QTreeWidgetItem] = {}

        explain = QPushButton("Explain")
        explain.setToolTip("Run EXPLAIN on the connected database")
        explain.clicked.connect(lambda: self.explain_requested.emit(self.analyze_box.isChecked()))
        self.analyze_box = QCheckBox("ANALYZE (executes the query)")
        paste = QPushButton("Paste plan…")
        paste.clicked.connect(self.paste_requested)
        load = QPushButton("Open plan file…")
        load.clicked.connect(self._open_file)
        self.export_btn = QPushButton("Export SVG…")
        self.export_btn.clicked.connect(self._export)
        fit = QPushButton("Fit")
        zin, zout = QPushButton("+"), QPushButton("−")
        for b in (zin, zout, fit):
            b.setFixedWidth(40)

        self.view = SvgView()
        self.view.show_message("Run Explain, or paste an existing plan, to visualize it.")
        fit.clicked.connect(self.view.fit)
        zin.clicked.connect(lambda: self.view.zoom(1.25))
        zout.clicked.connect(lambda: self.view.zoom(0.8))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(HEADERS)):
            self.tree.header().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._show_detail)
        self.detail = QTextBrowser()

        bottom = QSplitter(Qt.Orientation.Vertical)
        bottom.addWidget(self.tree)
        bottom.addWidget(self.detail)
        bottom.setStretchFactor(0, 3)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self.view)
        main.addWidget(bottom)
        main.setStretchFactor(0, 3)
        main.setStretchFactor(1, 2)

        bar = QHBoxLayout()
        for w in (explain, self.analyze_box, paste, load):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (zout, zin, fit, self.export_btn):
            bar.addWidget(w)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(main, 1)

    # ------------------------------------------------------------------------ data
    def set_plan(self, plan: Plan | None, findings: list[Finding]) -> None:
        self._plan = plan
        self._findings = findings
        self.tree.clear()
        self._items.clear()
        self.detail.clear()
        sev = severity_by_node(findings)
        show_plan_graph(self.view, plan, sev)
        if plan is None:
            return

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

    def _export(self) -> None:
        if not self.view.svg_bytes:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export plan", "plan.svg", "SVG (*.svg)")
        if path:
            with open(path, "wb") as fh:
                fh.write(self.view.svg_bytes)


def _h(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
