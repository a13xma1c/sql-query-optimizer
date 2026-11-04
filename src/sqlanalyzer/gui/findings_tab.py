"""Table of findings (errors, warnings, hints) with a detail pane."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
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

from ..models import Category, Finding, Severity
from ..optimizer import Suggestion

SEVERITY_COLORS = {Severity.ERROR: "#d73027", Severity.WARNING: "#e0a100", Severity.INFO: "#3b82d4"}
COLUMNS = ["Severity", "Category", "Line", "Rule", "Fix", "Message"]


class FindingsTab(QWidget):
    goto_requested = Signal(int, int)  # line, col
    node_requested = Signal(int)
    apply_requested = Signal(str)  # SQL with one suggested rewrite applied
    rules_requested = Signal()  # "Rules…" clicked: the owner opens FindingRulesDialog

    def __init__(self) -> None:
        super().__init__()
        self._findings: list[Finding] = []
        self._shown: list[Finding] = []
        self._fixable: set[str] = set()  # rules the optimizer resolves for the current query
        self._suggestions: list[Suggestion] = []  # individually applicable rewrites
        self._current_fix: Suggestion | None = None
        self.summary = QLabel("Run an analysis to see results.")
        self.filter = QComboBox()
        self.filter.addItem("All categories", None)
        for c in Category:
            self.filter.addItem(c.value.capitalize(), c)
        self.filter.currentIndexChanged.connect(self._render)
        self.rules_btn = QPushButton("Rules…")
        self.rules_btn.setToolTip("Choose which checks report findings")
        self.rules_btn.clicked.connect(self.rules_requested)

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
        self.detail.setMaximumHeight(180)
        self.fix_btn = QPushButton("Apply suggested rewrite")
        self.fix_btn.setToolTip("Replace the editor SQL with this single rewrite applied (Ctrl+Z undoes it)")
        self.fix_btn.setVisible(False)
        self.fix_btn.clicked.connect(self._apply_fix)
        detail_box = QWidget()
        dl = QVBoxLayout(detail_box)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.addWidget(self.detail, 1)
        fix_row = QHBoxLayout()
        fix_row.addStretch(1)
        fix_row.addWidget(self.fix_btn)
        dl.addLayout(fix_row)

        top = QHBoxLayout()
        top.addWidget(self.summary, 1)
        top.addWidget(self.filter)
        top.addWidget(self.rules_btn)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(detail_box)
        split.setStretchFactor(0, 1)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(split, 1)

    def set_findings(self, findings: list[Finding], fixable: set[str] | None = None,
                     suggestions: list[Suggestion] | None = None) -> None:
        """Show ``findings``; ``fixable`` names the rules the optimizer's rewrite resolves,
        ``suggestions`` are the per-rule rewrites offered as one-click fixes."""
        self._findings = findings
        self._fixable = set(fixable or ())
        self._suggestions = list(suggestions or ())
        errors = sum(f.severity is Severity.ERROR for f in findings)
        warns = sum(f.severity is Severity.WARNING for f in findings)
        infos = len(findings) - errors - warns
        text = ("<b>No issues found.</b>" if not findings
                else f"<b>{errors}</b> errors · <b>{warns}</b> warnings · <b>{infos}</b> hints")
        fixed = sum(f.rule in self._fixable for f in findings)
        if fixed:
            text += f" · <b>{fixed}</b> resolved by the Optimize tab"
        self.summary.setText(text)
        self._render()

    def _render(self) -> None:
        cat = self.filter.currentData()
        self._shown = [f for f in self._findings if cat is None or f.category == cat]
        self.table.setRowCount(len(self._shown))
        for row, f in enumerate(self._shown):
            sev = QTableWidgetItem(f.severity.value.upper())
            sev.setForeground(QBrush(QColor(SEVERITY_COLORS[f.severity])))
            sev.setData(Qt.ItemDataRole.FontRole, None)
            fix = QTableWidgetItem("✓" if f.rule in self._fixable else "")
            fix.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            fix.setForeground(QBrush(QColor("#1a7f37")))
            cells = [sev, QTableWidgetItem(f.category.value), QTableWidgetItem(str(f.line) if f.line else ""),
                     QTableWidgetItem(f.rule), fix, QTableWidgetItem(f.message)]
            for col, item in enumerate(cells):
                item.setToolTip("The Optimize tab rewrites this away." if col == 4 and fix.text() else f.message)
                self.table.setItem(row, col, item)
        self.detail.clear()
        self.fix_btn.setVisible(False)
        self._current_fix = None

    def suggestions_for(self, finding: Finding) -> list[Suggestion]:
        return [s for s in self._suggestions if s.addresses(finding.rule)]

    def _selected(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        f = self._shown[rows[0].row()]
        loc = f"line {f.line}, col {f.col}" if f.line else ("plan node" if f.node_id is not None else "")
        html = f"<p><b>{f.rule}</b> <span style='color:gray'>({f.category.value}{', ' + loc if loc else ''})</span></p>"
        html += f"<p>{_esc(f.message)}</p>"
        if f.suggestion:
            html += f"<p><b>Suggestion:</b> {_esc(f.suggestion)}</p>"
        fixes = self.suggestions_for(f)
        self._current_fix = fixes[0] if fixes else None
        if fixes:
            fix = fixes[0]
            rw = fix.rewrites[0]
            tag = "" if fix.safety.value == "safe" else " <span style='color:#bf8700'>[needs review]</span>"
            html += (f"<p><b>Suggested rewrite</b> ({_esc(fix.rule)}){tag}:<br>"
                     f"<code>{_esc(rw.before)}</code> &rarr; <code>{_esc(rw.after)}</code></p>")
        elif f.rule in self._fixable:
            html += "<p style='color:#1a7f37'><b>The rewrite in the Optimize tab resolves this finding.</b></p>"
        self.fix_btn.setVisible(bool(fixes))
        self.fix_btn.setText("Apply suggested rewrite" if not fixes or fixes[0].safety.value == "safe"
                             else "Apply suggested rewrite (review)")
        self.detail.setHtml(html)

    def _apply_fix(self) -> None:
        if self._current_fix is not None:
            self.apply_requested.emit(self._current_fix.sql)

    def _activate(self, row: int, _col: int) -> None:
        f = self._shown[row]
        if f.line:
            self.goto_requested.emit(f.line, f.col or 1)
        elif f.node_id is not None:
            self.node_requested.emit(f.node_id)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
