"""Rule-based optimization: rewritten SQL, the list of applied rewrites and rule settings."""

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
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..analyzer import QueryAnalysis
from ..optimizer import OPT_IN_RULES, RULE_CLASSES, OptimizationResult, Rewrite, Safety
from .editor import CodeEditor
from .session import Session

_SAFETY_COLORS = {Safety.SAFE: "#1a7f37", Safety.REVIEW: "#bf8700"}
COLUMNS = ["Rule", "Safety", "Line", "Category", "What changed"]


class RulesDialog(QDialog):
    """Enable/disable individual rewrite rules."""

    def __init__(self, disabled: set[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Optimization rules")
        self.resize(640, 520)
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
        self.apply_btn.setToolTip("Put the optimized SQL into the editor (Ctrl+Z undoes it)")
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

        bar = QHBoxLayout()
        bar.addWidget(self.review_box)
        bar.addWidget(rules_btn)
        bar.addStretch(1)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.apply_btn)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.summary)
        layout.addWidget(main, 1)

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
        self.apply_btn.setEnabled(False)
        self.copy_btn.setEnabled(False)

    def set_analysis(self, analysis: QueryAnalysis | None) -> None:
        self.table.setRowCount(0)
        self.detail.clear()
        self._shown = []
        result = analysis.optimization if analysis else None
        self._result = result
        if analysis is None or not analysis.syntax_ok:
            self.summary.setText("Optimization results appear once the SQL parses.")
            self.output.setPlainText("")
            self.apply_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            return
        if result is None:
            self.summary.setText("The optimizer could not process this query.")
            self.output.setPlainText("")
            self.apply_btn.setEnabled(False)
            self.copy_btn.setEnabled(False)
            return
        self.output.setPlainText(result.optimized_sql if result.changed else "")
        self.apply_btn.setEnabled(result.changed)
        self.copy_btn.setEnabled(result.changed)
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
        if self._result is not None and self._result.changed:
            self.apply_requested.emit(self._result.optimized_sql)

    def _copy(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._result is not None and self._result.changed:
            QApplication.clipboard().setText(self._result.optimized_sql)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
