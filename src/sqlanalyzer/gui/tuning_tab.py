"""Tuning: offline cost estimate, cost of the rewrite, and index / physical-design recommendations."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..analyzer import QueryAnalysis
from ..indexes import IndexRecommendation
from ..plans.costing import fmt

_PRIORITY_COLORS = {"high": "#d73027", "medium": "#bf8700", "low": "#57606a"}
COLUMNS = ["Priority", "Kind", "Table", "Columns", "Why"]


class TuningTab(QWidget):
    node_requested = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._recs: list[IndexRecommendation] = []
        self.cost_view = QTextBrowser()
        self.cost_view.setOpenLinks(False)
        self.cost_view.anchorClicked.connect(self._anchor)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selected)
        self.ddl = QTextBrowser()
        self.ddl.setPlaceholderText("Select a recommendation to see the DDL.")
        self.copy_btn = QPushButton("Copy DDL")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy)
        self.copy_all_btn = QPushButton("Copy all DDL")
        self.copy_all_btn.setEnabled(False)
        self.copy_all_btn.clicked.connect(self._copy_all)
        self.summary = QLabel("Index recommendations appear once the SQL parses.")
        self.summary.setWordWrap(True)

        bar = QHBoxLayout()
        bar.addWidget(self.summary, 1)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.copy_all_btn)
        indexes = QWidget()
        il = QVBoxLayout(indexes)
        il.setContentsMargins(0, 0, 0, 0)
        il.addLayout(bar)
        il.addWidget(self.table, 2)
        il.addWidget(self.ddl, 1)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.cost_view)
        split.addWidget(indexes)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        layout = QVBoxLayout(self)
        layout.addWidget(split, 1)

    # ---------------------------------------------------------------------- data
    def set_analysis(self, analysis: QueryAnalysis | None) -> None:
        self.table.setRowCount(0)
        self.ddl.clear()
        self._recs = []
        if analysis is None or not analysis.syntax_ok:
            self.cost_view.setHtml("<p style='color:gray'>The cost estimate appears once the SQL parses.</p>")
            self.summary.setText("Index recommendations appear once the SQL parses.")
            self.copy_btn.setEnabled(False)
            self.copy_all_btn.setEnabled(False)
            return
        self.cost_view.setHtml(self._cost_html(analysis))
        self._recs = list(analysis.index_recommendations)
        self.table.setRowCount(len(self._recs))
        for row, rec in enumerate(self._recs):
            prio = QTableWidgetItem(rec.priority.upper())
            prio.setForeground(QBrush(QColor(_PRIORITY_COLORS[rec.priority])))
            cells = [prio, QTableWidgetItem(rec.kind), QTableWidgetItem(rec.table),
                     QTableWidgetItem(", ".join(rec.columns)), QTableWidgetItem(rec.reason)]
            for col, item in enumerate(cells):
                item.setToolTip(rec.ddl)
                self.table.setItem(row, col, item)
        high = sum(r.priority == "high" for r in self._recs)
        if self._recs:
            text = f"<b>{len(self._recs)}</b> recommendation(s), {high} high priority."
            if analysis.cost_change_with_indexes:
                text += f" With them, the estimated cost changes {_esc(analysis.cost_change_with_indexes)}."
        else:
            text = "No index or physical-design recommendation for this query."
        self.summary.setText(text)
        self.copy_all_btn.setEnabled(bool(self._recs))
        self.copy_btn.setEnabled(False)

    def _cost_html(self, analysis: QueryAnalysis) -> str:
        cost = analysis.cost
        if cost is None:
            return "<p style='color:gray'>No cost estimate (the statement has no plan).</p>"
        parts = [f"<p><b>Offline cost model</b> - {_esc(cost.summary())}</p>"]
        if analysis.cost_change:
            parts.append(f"<p>Rule-engine rewrite: <b>{_esc(analysis.cost_change)}</b></p>")
        elif analysis.optimization is not None and analysis.optimization.changed:
            parts.append("<p>Rule-engine rewrite: no cost change under the model.</p>")
        if analysis.cost_change_with_indexes:
            parts.append(f"<p>With the recommended indexes: <b>{_esc(analysis.cost_change_with_indexes)}</b></p>")
        if cost.hottest:
            parts.append("<p><b>Most expensive operators</b></p><ul>")
            for node in cost.hottest:
                share = cost.share(node.node_id)
                parts.append(f"<li><a href='node:{node.node_id}'>{_esc(node.op)}</a>: {fmt(node.self_cost)} units "
                             f"({share:.0%}), ~{fmt(node.rows)} rows"
                             + (f" - {_esc(node.note)}" if node.note else "") + "</li>")
            parts.append("</ul>")
        if cost.table_rows:
            rows = ", ".join(f"{_esc(t)} ~{fmt(r)}" for t, r in cost.table_rows.items())
            parts.append(f"<p>Table sizes used: {rows}.</p>")
        if cost.assumptions:
            parts.append("<p><b>Assumptions</b> (declare statistics in the Schema tab to tighten them)</p><ul>")
            parts += [f"<li>{_esc(a)}</li>" for a in cost.assumptions]
            parts.append("</ul>")
        parts.append("<p style='color:gray'>Cost units are relative: they rank alternatives and point at the "
                     "dominant operator; they are not milliseconds.</p>")
        return "".join(parts)

    # ------------------------------------------------------------------ actions
    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        rec = self._recs[rows[0].row()]
        text = rec.ddl
        if rec.fixes:
            text += f"\n\n-- addresses: {', '.join(rec.fixes)}"
        if rec.table_rows:
            text += f"\n-- table size used: ~{fmt(rec.table_rows)} rows"
        self.ddl.setPlainText(text)
        self.copy_btn.setEnabled(True)

    def _copy(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if rows:
            QApplication.clipboard().setText(self._recs[rows[0].row()].ddl)

    def _copy_all(self) -> None:
        QApplication.clipboard().setText("\n".join(r.ddl for r in self._recs))

    def _anchor(self, url) -> None:
        text = url.toString()
        if text.startswith("node:"):
            self.node_requested.emit(int(text.split(":", 1)[1]))


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
