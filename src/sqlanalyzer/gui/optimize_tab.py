"""Query optimization: the deterministic rule engine and, optionally, a validated local LLM."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..analyzer import QueryAnalysis
from ..dialects import ENGINES
from ..optimizer import OPT_IN_RULES, RULE_CLASSES, OptimizationResult, Rewrite, Safety, Suggestion, apply_rules
from .editor import CodeEditor
from .geometry import fit_dialog
from .llm_panel import LlmPanel
from .session import Session

_SAFETY_COLORS = {Safety.SAFE: "#1a7f37", Safety.REVIEW: "#bf8700"}
COLUMNS = ["Rule", "Safety", "Line", "Category", "What changed"]


class RulesDialog(QDialog):
    """Enable/disable individual rewrite rules."""

    def __init__(self, disabled: set[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Optimization rules")
        fit_dialog(self, 640, 520)
        self._boxes: dict[str, QCheckBox] = {}
        inner = QWidget()
        form = QVBoxLayout(inner)
        for cls in RULE_CLASSES:
            box = QCheckBox(f"{cls.title}  [{cls.name}]")
            box.setChecked(cls.name not in disabled)
            note = cls.description.replace("``", "")
            if cls.safety is Safety.REVIEW:
                note = "Needs review. " + note
            if cls.requires_qualified:
                note += " Requires a declared schema (or fully qualified columns)."
            if cls.name in OPT_IN_RULES:
                note += " Off by default."
            box.setToolTip(note)
            label = QLabel(note)
            label.setWordWrap(True)
            label.setStyleSheet("color: gray; margin-left: 24px; margin-bottom: 6px;")
            form.addWidget(box)
            form.addWidget(label)
            self._boxes[cls.name] = box
        form.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    def disabled_rules(self) -> set[str]:
        return {name for name, box in self._boxes.items() if not box.isChecked()}


class SuggestionsPage(QWidget):
    """Pick individual rewrites instead of applying the whole rule engine result."""

    changed = Signal()

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._analysis: QueryAnalysis | None = None
        self._suggestions: list[Suggestion] = []
        self.info = QLabel("Tick the rewrites you want; the preview shows the query with exactly those applied.")
        self.info.setWordWrap(True)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Apply", "Rule", "Safety", "Line", "Change"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemChanged.connect(self._item_changed)
        self.table.itemSelectionChanged.connect(self._selected)
        self.detail = QTextBrowser()
        self.preview = CodeEditor("Preview of the selected rewrites…")
        self.preview.setReadOnly(True)
        self.select_all = QPushButton("Select safe")
        self.select_all.clicked.connect(lambda: self._select(lambda s: s.safety is Safety.SAFE))
        self.select_none = QPushButton("Select none")
        self.select_none.clicked.connect(lambda: self._select(lambda s: False))

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.table)
        right.addWidget(self.detail)
        right.setStretchFactor(0, 2)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self.preview)
        main.addWidget(right)
        bar = QHBoxLayout()
        bar.addWidget(self.info, 1)
        bar.addWidget(self.select_all)
        bar.addWidget(self.select_none)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(main, 1)

    def set_analysis(self, analysis: QueryAnalysis | None) -> None:
        self._analysis = analysis
        self._suggestions = list(analysis.suggestions) if analysis and analysis.syntax_ok else []
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._suggestions))
        for row, s in enumerate(self._suggestions):
            tick = QTableWidgetItem()
            tick.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            tick.setCheckState(Qt.CheckState.Unchecked)
            safety = QTableWidgetItem(s.safety.value.upper())
            safety.setForeground(QBrush(QColor(_SAFETY_COLORS[s.safety])))
            cells = [tick, QTableWidgetItem(s.rule), safety, QTableWidgetItem(str(s.line) if s.line else ""),
                     QTableWidgetItem(s.summary)]
            for col, item in enumerate(cells):
                item.setToolTip(s.description)
                self.table.setItem(row, col, item)
        self.table.blockSignals(False)
        self.detail.clear()
        self.preview.setPlainText("")
        if not self._suggestions:
            self.info.setText("No individual rewrite suggestions for this query."
                              if analysis and analysis.syntax_ok else "Suggestions appear once the SQL parses.")
        else:
            self.info.setText(f"{len(self._suggestions)} suggestion(s). Tick the ones you want; the preview shows "
                              "the query with exactly those applied.")
        self.changed.emit()

    def selected_rules(self) -> list[str]:
        return [s.rule for row, s in enumerate(self._suggestions)
                if (item := self.table.item(row, 0)) is not None and item.checkState() == Qt.CheckState.Checked]

    def preview_sql(self) -> str:
        """Query with the ticked rewrites applied ('' when nothing is ticked)."""
        names = self.selected_rules()
        if not names or self._analysis is None:
            return ""
        dialect = ENGINES[self._analysis.engine].sqlglot_dialect
        result = apply_rules(self._analysis.statements, dialect, names, schema=self.session.schema,
                             include_review=True)
        return result.optimized_sql if result.changed else ""

    def _select(self, predicate) -> None:
        self.table.blockSignals(True)
        for row, s in enumerate(self._suggestions):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if predicate(s) else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._item_changed(None)

    def _item_changed(self, _item) -> None:
        self.preview.setPlainText(self.preview_sql())
        self.changed.emit()

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        s = self._suggestions[rows[0].row()]
        html = f"<p><b>{_esc(s.title)}</b> <span style='color:gray'>{s.rule} · {s.category}</span></p>"
        html += f"<p>{_esc(s.description)}</p><ul>"
        for rw in s.rewrites:
            html += f"<li><code>{_esc(rw.before)}</code> &rarr; <code>{_esc(rw.after)}</code><br>{_esc(rw.description)}</li>"
        html += "</ul>"
        if s.fixes:
            html += f"<p style='color:gray'>Addresses finding(s): {_esc(', '.join(s.fixes))}</p>"
        self.detail.setHtml(html)


class OptimizeTab(QWidget):
    apply_requested = Signal(str)  # optimized SQL to put into the editor
    goto_requested = Signal(int, int)

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._result: OptimizationResult | None = None
        self._shown: list[Rewrite] = []

        self.summary = QLabel("Optimization results appear once the SQL parses.")
        self.summary.setWordWrap(True)
        self.review_box = QCheckBox("Include rewrites that need review")
        self.review_box.setToolTip("Rewrites such as UNION -> UNION ALL or NOT IN -> NOT EXISTS can change "
                                   "results in edge cases; untick to apply only semantics-preserving rules.")
        self.review_box.setChecked(session.include_review)
        self.review_box.toggled.connect(self._settings_changed)
        rules_btn = QPushButton("Rules…")
        rules_btn.clicked.connect(self._edit_rules)
        self.apply_btn = QPushButton("Replace editor SQL")
        self.apply_btn.setToolTip("Put the selected rewrite into the editor (Ctrl+Z undoes it)")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self._apply)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setEnabled(False)
        self.copy_btn.clicked.connect(self._copy)

        self.output = CodeEditor("Optimized SQL…")
        self.output.setReadOnly(True)

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
        self.table.cellDoubleClicked.connect(self._activate)
        self.detail = QTextBrowser()

        right = QSplitter(Qt.Orientation.Vertical)
        right.addWidget(self.table)
        right.addWidget(self.detail)
        right.setStretchFactor(0, 2)
        right.setStretchFactor(1, 1)
        main = QSplitter(Qt.Orientation.Horizontal)
        main.addWidget(self.output)
        main.addWidget(right)
        main.setStretchFactor(0, 1)
        main.setStretchFactor(1, 1)

        rules_page = QWidget()
        rules_layout = QVBoxLayout(rules_page)
        rules_layout.setContentsMargins(0, 0, 0, 0)
        rules_layout.addWidget(self.summary)
        rules_layout.addWidget(main, 1)

        self.llm = LlmPanel(session)
        self.llm.changed.connect(self._refresh_buttons)
        self.picker = SuggestionsPage(session)
        self.picker.changed.connect(self._refresh_buttons)
        self.pages = QTabWidget()
        self.pages.addTab(rules_page, "Rule engine")
        self.pages.addTab(self.picker, "Pick rewrites")
        self.pages.addTab(self.llm, "Local LLM (validated)")
        self.pages.currentChanged.connect(self._refresh_buttons)

        bar = QHBoxLayout()
        bar.addWidget(self.review_box)
        bar.addWidget(rules_btn)
        bar.addStretch(1)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.apply_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.pages, 1)

    # ------------------------------------------------------------------- buttons
    @property
    def llm_page_active(self) -> bool:
        return self.pages.currentWidget() is self.llm

    def current_sql(self) -> str:
        """SQL of the active page, empty when there is nothing applicable."""
        if self.llm_page_active:
            return self.llm.applicable_sql()
        if self.pages.currentWidget() is self.picker:
            return self.picker.preview_sql()
        return self._result.optimized_sql if self._result is not None and self._result.changed else ""

    def _refresh_buttons(self) -> None:
        usable = bool(self.current_sql())
        self.apply_btn.setEnabled(usable)
        self.copy_btn.setEnabled(usable)
        if self.llm_page_active and not usable:
            result = self.llm.result
            if result is not None and result.review is not None and not result.review.ok:
                self.apply_btn.setToolTip("The model's rewrite failed validation and cannot be applied.")
            elif self.llm.stale:
                self.apply_btn.setToolTip("The query changed; ask the model again.")
            else:
                self.apply_btn.setToolTip("Ask the local LLM first.")
        else:
            self.apply_btn.setToolTip("Put the selected rewrite into the editor (Ctrl+Z undoes it)")

    # ------------------------------------------------------------------ settings
    def _settings_changed(self) -> None:
        self.session.save_optimizer(self.review_box.isChecked(), self.session.disabled_rules)

    def _edit_rules(self) -> None:
        dlg = RulesDialog(self.session.disabled_rules, self)
        if dlg.exec():
            self.session.save_optimizer(self.review_box.isChecked(), dlg.disabled_rules())

    # ---------------------------------------------------------------------- data
    def set_pending(self) -> None:
        self.summary.setText("Optimizing…")
        self._refresh_buttons()

    def set_analysis(self, analysis: QueryAnalysis | None) -> None:
        self.table.setRowCount(0)
        self.detail.clear()
        self._shown = []
        result = analysis.optimization if analysis else None
        self._result = result
        self.llm.set_analysis(analysis)
        self.picker.set_analysis(analysis)
        if analysis is None or not analysis.syntax_ok:
            self.summary.setText("Optimization results appear once the SQL parses.")
            self.output.setPlainText("")
            self._refresh_buttons()
            return
        if result is None:
            self.summary.setText("The optimizer could not process this query.")
            self.output.setPlainText("")
            self._refresh_buttons()
            return
        self.output.setPlainText(result.optimized_sql if result.changed else "")
        self._refresh_buttons()
        self.summary.setText(self._summary_html(analysis, result))
        self._shown = list(result.rewrites)
        self.table.setRowCount(len(self._shown))
        for row, rw in enumerate(self._shown):
            sev = QTableWidgetItem(rw.safety.value.upper())
            sev.setForeground(QBrush(QColor(_SAFETY_COLORS[rw.safety])))
            cells = [QTableWidgetItem(rw.rule), sev, QTableWidgetItem(str(rw.line) if rw.line else ""),
                     QTableWidgetItem(rw.category), QTableWidgetItem(rw.description)]
            for col, item in enumerate(cells):
                item.setToolTip(rw.description)
                self.table.setItem(row, col, item)

    def _summary_html(self, analysis: QueryAnalysis, result: OptimizationResult) -> str:
        parts = [f"<b>{_esc(result.summary())}</b>"]
        resolved = analysis.resolved_by_optimization
        if resolved:
            names = sorted({f.rule for f in resolved})
            parts.append(f"Resolves {len(resolved)} finding(s): {_esc(', '.join(names))}.")
        if result.qualified:
            parts.append("Columns were resolved against the schema, so all rules ran.")
        else:
            n = len([k for k, v in result.skipped.items() if v.startswith("needs every column")])
            if n:
                parts.append(f"{n} schema-dependent rule(s) skipped: declare tables in the <i>Schema</i> tab "
                             "(or qualify every column and avoid SELECT *) to enable predicate/projection "
                             "pushdown, subquery merging and join elimination.")
        errors = self.session.schema_hints.errors
        if errors:
            parts.append(f"<span style='color:#d73027'>Schema hints: {_esc(errors[0])}</span>")
        if result.errors:
            parts.append(f"<span style='color:gray'>{len(result.errors)} rewrite(s) discarded by validation.</span>")
        return " ".join(parts)

    # ------------------------------------------------------------------ actions
    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        rw = self._shown[rows[0].row()]
        color = _SAFETY_COLORS[rw.safety]
        html = (f"<p><b>{_esc(rw.title)}</b> <span style='color:{color}'>[{rw.safety.value}]</span>"
                f" <span style='color:gray'>{rw.rule}" + (f", line {rw.line}" if rw.line else "") + "</span></p>")
        html += f"<p>{_esc(rw.description)}</p>"
        html += f"<p><b>Before:</b><br><code>{_esc(rw.before)}</code></p>"
        html += f"<p><b>After:</b><br><code>{_esc(rw.after)}</code></p>"
        if rw.fixes:
            html += f"<p style='color:gray'>Addresses finding(s): {_esc(', '.join(rw.fixes))}</p>"
        self.detail.setHtml(html)

    def _activate(self, row: int, _col: int) -> None:
        rw = self._shown[row]
        if rw.line:
            self.goto_requested.emit(rw.line, rw.col or 1)

    def _apply(self) -> None:
        sql = self.current_sql()
        if sql:
            self.apply_requested.emit(sql)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        sql = self.current_sql()
        if sql:
            QApplication.clipboard().setText(sql)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
