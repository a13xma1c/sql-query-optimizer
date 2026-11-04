"""Query history: every explicitly analyzed / explained / rewritten query, searchable and reloadable."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..dialects import ENGINES, Engine
from ..history import HistoryEntry, QueryHistory
from ..plans.costing import fmt
from .editor import CodeEditor

COLUMNS = ["When", "Engine", "Runs", "Findings", "Score", "Est. cost", "Source", "Query"]


class HistoryTab(QWidget):
    load_requested = Signal(str, object)  # sql, engine
    compare_requested = Signal(str, bool)  # sql, as_old

    def __init__(self, history: QueryHistory) -> None:
        super().__init__()
        self.history = history
        self._shown: list[HistoryEntry] = []

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search queries and tags…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.refresh)
        self.engine_filter = QComboBox()
        self.engine_filter.addItem("All engines", None)
        for engine, info in ENGINES.items():
            self.engine_filter.addItem(info.label, engine.value)
        self.engine_filter.currentIndexChanged.connect(self.refresh)
        self.count = QLabel("")

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
        self.table.cellDoubleClicked.connect(lambda *_: self.load_selected())
        self.preview = CodeEditor("Select an entry to preview it…")
        self.preview.setReadOnly(True)

        self.load_btn = QPushButton("Load into editor")
        self.load_btn.clicked.connect(self.load_selected)
        self.a_btn = QPushButton("Compare as A")
        self.a_btn.clicked.connect(lambda: self._compare(True))
        self.b_btn = QPushButton("Compare as B")
        self.b_btn.clicked.connect(lambda: self._compare(False))
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("tags, comma separated")
        self.tag_btn = QPushButton("Save tags")
        self.tag_btn.clicked.connect(self._save_tags)
        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self.delete_selected)
        export_btn = QPushButton("Export…")
        export_btn.clicked.connect(self._export)
        clear_btn = QPushButton("Clear history")
        clear_btn.clicked.connect(self.clear)
        for b in (self.load_btn, self.a_btn, self.b_btn, self.tag_btn, self.delete_btn):
            b.setEnabled(False)

        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.engine_filter)
        top.addWidget(self.count)
        actions = QHBoxLayout()
        for w in (self.load_btn, self.a_btn, self.b_btn):
            actions.addWidget(w)
        actions.addWidget(self.tag_edit, 1)
        actions.addWidget(self.tag_btn)
        actions.addStretch(1)
        for w in (self.delete_btn, export_btn, clear_btn):
            actions.addWidget(w)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.preview)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)
        layout.addLayout(actions)
        self.refresh()

    # ---------------------------------------------------------------------- data
    def refresh(self) -> None:
        self._shown = self.history.entries(engine=self.engine_filter.currentData(), query=self.search.text())
        self.table.setRowCount(len(self._shown))
        for row, e in enumerate(self._shown):
            cells = [e.last_seen_local, ENGINES[Engine(e.engine)].label if e.engine in Engine._value2member_map_
                     else e.engine, str(e.runs), str(e.findings), "" if e.complexity is None else str(e.complexity),
                     "" if e.cost is None else fmt(e.cost), e.source, e.title + (f"  [{', '.join(e.tags)}]" if e.tags else "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(e.sql)
                if col in (2, 3, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
        self.count.setText(f"{len(self._shown)} of {len(self.history)}")
        self._selected()

    def selected(self) -> HistoryEntry | None:
        rows = self.table.selectionModel().selectedRows()
        return self._shown[rows[0].row()] if rows else None

    def _selected(self) -> None:
        entry = self.selected()
        for b in (self.load_btn, self.a_btn, self.b_btn, self.tag_btn, self.delete_btn):
            b.setEnabled(entry is not None)
        self.preview.setPlainText(entry.sql if entry else "")
        self.tag_edit.setText(", ".join(entry.tags) if entry else "")

    # ------------------------------------------------------------------ actions
    def load_selected(self) -> None:
        entry = self.selected()
        if entry is not None:
            self.load_requested.emit(entry.sql, entry.engine)

    def _compare(self, as_old: bool) -> None:
        entry = self.selected()
        if entry is not None:
            self.compare_requested.emit(entry.sql, as_old)

    def _save_tags(self) -> None:
        entry = self.selected()
        if entry is not None:
            tags = [t.strip() for t in self.tag_edit.text().split(",") if t.strip()]
            self.history.tag(entry.id, tags)
            self.refresh()

    def delete_selected(self) -> None:
        entry = self.selected()
        if entry is not None:
            self.history.remove(entry.id)
            self.refresh()

    def clear(self) -> None:
        if not len(self.history):
            return
        answer = QMessageBox.question(self, "Clear history", f"Delete all {len(self.history)} history entries?")
        if answer == QMessageBox.StandardButton.Yes:
            self.history.clear()
            self.refresh()

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export history", "query_history.sql", "SQL (*.sql)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.history.export_sql())
