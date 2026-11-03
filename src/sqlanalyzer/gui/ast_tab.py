"""Abstract syntax tree viewer: tree widget + optional Graphviz drawing."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ast import AstNode, ast_stats, ast_to_json, ast_to_text, graphviz_available, render_ast_svg
from .svg_view import SvgView

HEADERS = ["Node", "Slot", "SQL"]
MAX_GRAPH_NODES = 300


class AstTab(QWidget):
    goto_requested = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._trees: list[AstNode] = []
        self._items: dict[int, QTreeWidgetItem] = {}
        self._dirty = False

        self.stats = QLabel("The syntax tree appears once the SQL parses.")
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by node kind (e.g. Join, Column, Subquery)…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._apply_filter)
        self.graph_box = QCheckBox("Draw graph")
        self.graph_box.setToolTip("Render the tree with Graphviz (large trees are truncated)")
        self.graph_box.toggled.connect(self._render_graph)
        expand = QPushButton("Expand all")
        expand.clicked.connect(lambda: self.tree.expandAll())
        collapse = QPushButton("Collapse")
        collapse.clicked.connect(self._collapse)
        export = QPushButton("Export JSON…")
        export.clicked.connect(self._export)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(HEADERS)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._selected)
        self.tree.itemDoubleClicked.connect(self._activated)
        self.view = SvgView()
        self.view.show_message("Tick 'Draw graph' to render the tree with Graphviz.")
        self.view.setVisible(False)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.tree)
        split.addWidget(self.view)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        bar = QHBoxLayout()
        bar.addWidget(self.filter, 1)
        for w in (expand, collapse, export, self.graph_box):
            bar.addWidget(w)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.stats)
        layout.addWidget(split, 1)

    # ---------------------------------------------------------------------- data
    def set_trees(self, trees: list[AstNode]) -> None:
        self._trees = trees
        self._dirty = True
        if self.isVisible():
            self._render()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if self._dirty:
            self._render()

    def _render(self) -> None:
        self._dirty = False
        self.tree.clear()
        self._items.clear()
        if not self._trees:
            self.stats.setText("The syntax tree appears once the SQL parses.")
            self.view.show_message("No tree.")
            return
        for i, root in enumerate(self._trees):
            item = self._add(root, None, i)
            self.tree.addTopLevelItem(item)
        self.tree.expandToDepth(3)
        totals = [ast_stats(t) for t in self._trees]
        nodes = sum(s.nodes for s in totals)
        depth = max(s.depth for s in totals)
        top = ", ".join(f"{k} ×{n}" for k, n in totals[0].top_kinds[:6])
        self.stats.setText(f"{len(self._trees)} statement(s) · {nodes} nodes · depth {depth} · {top}")
        self._apply_filter(self.filter.text())
        self._render_graph()

    def _add(self, node: AstNode, parent: QTreeWidgetItem | None, stmt: int) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.title, node.arg, node.sql])
        item.setData(0, Qt.ItemDataRole.UserRole, (stmt, node.id))
        item.setToolTip(2, node.sql)
        self._items[(stmt, node.id)] = item  # type: ignore[index]
        if parent is not None:
            parent.addChild(item)
        for child in node.children:
            self._add(child, item, stmt)
        return item

    def _apply_filter(self, text: str) -> None:
        text = text.strip().lower()
        for (stmt, nid), item in self._items.items():
            node = self._node(stmt, nid)
            match = not text or text in node.kind.lower() or text in node.label.lower()
            item.setHidden(bool(text) and not match)
            if text and match:
                p = item.parent()
                while p is not None:
                    p.setHidden(False)
                    p.setExpanded(True)
                    p = p.parent()

    def _node(self, stmt: int, nid: int) -> AstNode:
        for n in self._trees[stmt].walk():
            if n.id == nid:
                return n
        raise KeyError(nid)

    def _collapse(self) -> None:
        self.tree.collapseAll()
        self.tree.expandToDepth(1)

    def _render_graph(self) -> None:
        show = self.graph_box.isChecked()
        self.view.setVisible(show)
        if not show:
            return
        if not self._trees:
            self.view.show_message("No tree.")
            return
        if not graphviz_available():
            self.view.show_message("Graphviz 'dot' not found; install Graphviz to draw the tree.")
            return
        try:
            self.view.load_svg(render_ast_svg(self._trees[0], MAX_GRAPH_NODES))
        except Exception as exc:  # noqa: BLE001 - dot failures are not fatal
            self.view.show_message(f"Graphviz failed: {exc}")

    # ------------------------------------------------------------------ actions
    def _selected(self) -> None:
        items = self.tree.selectedItems()
        if not items:
            return
        stmt, nid = items[0].data(0, Qt.ItemDataRole.UserRole)
        node = self._node(stmt, nid)
        self.tree.setToolTip(node.sql)

    def _activated(self, item: QTreeWidgetItem, _col: int) -> None:
        stmt, nid = item.data(0, Qt.ItemDataRole.UserRole)
        node = self._node(stmt, nid)
        if node.line:
            self.goto_requested.emit(node.line, node.col or 1)

    def _export(self) -> None:
        if not self._trees:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export AST", "ast.json", "JSON (*.json);;Text (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            if path.endswith(".txt"):
                fh.write("\n\n".join(ast_to_text(t, with_sql=True) for t in self._trees))
            else:
                fh.write("[" + ",\n".join(ast_to_json(t) for t in self._trees) + "]")
