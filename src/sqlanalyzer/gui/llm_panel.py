"""Ask a local LLM for a rewrite and show the validation against the rule set."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
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
from ..llm import FAIL, PASS, WARN, LlmClient, LlmOptimization, llm_optimize
from .editor import CodeEditor
from .geometry import fit_dialog
from .llm_dialog import LlmSettingsDialog
from .session import Session
from .workers import run_async

STATUS_COLORS = {PASS: "#1a7f37", WARN: "#bf8700", FAIL: "#d73027"}
CHECK_COLUMNS = ["Check", "Result", "Detail"]


class _TextDialog(QDialog):
    def __init__(self, title: str, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        fit_dialog(self, 820, 620)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout = QVBoxLayout(self)
        layout.addWidget(view, 1)
        layout.addWidget(buttons)


class LlmPanel(QWidget):
    """Local-LLM suggestion: request, result, and the checks it had to pass."""

    changed = Signal()  # a new (or cleared) suggestion is available
    goto_requested = Signal(int, int)

    def __init__(self, session: Session) -> None:
        super().__init__()
        self.session = session
        self._analysis: QueryAnalysis | None = None
        self._result: LlmOptimization | None = None
        self._sql_at_request = ""
        self.busy = False

        self.ask_btn = QPushButton("Ask local LLM")
        self.ask_btn.setToolTip("Send the query, the declared schema and the findings to the configured local model")
        self.ask_btn.clicked.connect(self.ask)
        settings_btn = QPushButton("Settings…")
        settings_btn.clicked.connect(self.edit_settings)
        self.prompt_btn = QPushButton("Prompt / answer…")
        self.prompt_btn.setEnabled(False)
        self.prompt_btn.clicked.connect(self._show_exchange)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.output = CodeEditor("The model's rewrite appears here…")
        self.output.setReadOnly(True)

        self.checks = QTableWidget(0, len(CHECK_COLUMNS))
        self.checks.setHorizontalHeaderLabels(CHECK_COLUMNS)
        self.checks.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.checks.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.checks.verticalHeader().setVisible(False)
        self.checks.setAlternatingRowColors(True)
        hdr = self.checks.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(len(CHECK_COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.changes_view = QTextBrowser()
        self.panes = QTabWidget()
        self.panes.addTab(self.checks, "Validation")
        self.panes.addTab(self.changes_view, "What the model says")

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.output)
        split.addWidget(self.panes)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)

        bar = QHBoxLayout()
        for w in (self.ask_btn, settings_btn, self.prompt_btn):
            bar.addWidget(w)
        bar.addStretch(1)
        layout = QVBoxLayout(self)
        layout.addLayout(bar)
        layout.addWidget(self.status)
        layout.addWidget(split, 1)
        session.llm_changed.connect(self._refresh_status)
        self._refresh_status()

    # --------------------------------------------------------------------- data
    def set_analysis(self, analysis: QueryAnalysis | None) -> None:
        self._analysis = analysis
        self.ask_btn.setEnabled(bool(analysis and analysis.syntax_ok) and not self.busy)
        if self._result is not None and analysis is not None and analysis.sql != self._sql_at_request:
            self._refresh_status(stale=True)
            self.changed.emit()

    @property
    def result(self) -> LlmOptimization | None:
        return self._result

    @property
    def stale(self) -> bool:
        return bool(self._result and self._analysis and self._analysis.sql != self._sql_at_request)

    def applicable_sql(self) -> str:
        """The model's SQL, but only when it passed validation and still matches the editor."""
        if self._result is None or not self._result.ok or self.stale:
            return ""
        return self._result.sql

    # ------------------------------------------------------------------ actions
    def edit_settings(self) -> None:
        dialog = LlmSettingsDialog(self.session.llm, self)
        if dialog.exec():
            self.session.save_llm(dialog.config())
            self._refresh_status()

    def ask(self) -> None:
        analysis = self._analysis
        if analysis is None or not analysis.syntax_ok or self.busy:
            return
        config = self.session.llm
        dialect = ENGINES[analysis.engine].sqlglot_dialect
        client = LlmClient(config)
        sql = analysis.sql
        self._sql_at_request = sql
        self.busy = True
        self.ask_btn.setEnabled(False)
        self.status.setText(f"Asking {config.display()} … (rewrites are validated against the rule set "
                            "before they can be applied)")

        schema_text, partition_text = self.session.schema_text, self.session.hints_text
        schema, hints = self.session.schema, self.session.hints
        findings = list(analysis.findings)
        rules = analysis.optimization

        def work() -> LlmOptimization:
            return llm_optimize(sql, dialect, client, schema=schema, schema_text=schema_text,
                                partition_text=partition_text, partition_hints=hints, findings=findings,
                                rules=rules)

        def done(result: LlmOptimization) -> None:
            self.busy = False
            self.ask_btn.setEnabled(True)
            self._show(result)

        def failed(message: str) -> None:
            self.busy = False
            self.ask_btn.setEnabled(True)
            self._result = None
            self.status.setText(f"<span style='color:#d73027'>Local LLM failed: {_esc(message)}</span>")
            self.changed.emit()

        run_async(self, work, done, failed)

    # ------------------------------------------------------------------ display
    def _show(self, result: LlmOptimization) -> None:
        self._result = result
        self.prompt_btn.setEnabled(True)
        self.output.setPlainText(result.sql)
        self.checks.setRowCount(0)
        review = result.review
        if review is not None:
            self.checks.setRowCount(len(review.checks))
            for row, check in enumerate(review.checks):
                status = QTableWidgetItem(check.status.upper())
                status.setForeground(QBrush(QColor(STATUS_COLORS[check.status])))
                cells = [QTableWidgetItem(check.name), status, QTableWidgetItem(check.detail)]
                for col, item in enumerate(cells):
                    item.setToolTip(check.detail)
                    self.checks.setItem(row, col, item)
        self.changes_view.setHtml(self._changes_html(result))
        self._refresh_status()
        self.changed.emit()

    def _changes_html(self, result: LlmOptimization) -> str:
        suggestion = result.suggestion
        if suggestion is None:
            return f"<p style='color:#d73027'>{_esc(result.error or 'No answer.')}</p>"
        parts = []
        if suggestion.changes:
            parts.append("<p><b>Changes the model reports</b></p><ul>")
            for change in suggestion.changes:
                risk = str(change.get("risk", "")).strip()
                tag = (f" <span style='color:#bf8700'>[{_esc(risk)}]</span>"
                       if risk and risk.lower() not in ("none", "no", "safe") else "")
                parts.append(f"<li><b>{_esc(str(change.get('title', 'change')))}</b>{tag}<br>"
                             f"{_esc(str(change.get('reason', '')))}</li>")
            parts.append("</ul>")
        if suggestion.notes:
            parts.append(f"<p><b>Notes:</b> {_esc(suggestion.notes)}</p>")
        if suggestion.parse_error:
            parts.append(f"<p style='color:#bf8700'>{_esc(suggestion.parse_error)}</p>")
        review = result.review
        if review is not None:
            if review.resolved:
                parts.append("<p><b>Findings resolved:</b> "
                             + _esc(", ".join(sorted({f.rule for f in review.resolved}))) + "</p>")
            if review.introduced:
                parts.append("<p style='color:#bf8700'><b>Findings introduced:</b> "
                             + _esc(", ".join(sorted({f.rule for f in review.introduced}))) + "</p>")
            if review.missed_rules:
                parts.append("<p><b>Rule engine can still improve it:</b> "
                             + _esc(", ".join(review.missed_rules)) + "</p>")
        return "".join(parts) or "<p style='color:gray'>The model reported no details.</p>"

    def _refresh_status(self, stale: bool = False) -> None:
        config = self.session.llm
        result = self._result
        if result is None:
            self.status.setText(f"No suggestion yet. Configured model: <code>{_esc(config.display())}</code>. "
                                "Nothing is sent anywhere until you press “Ask local LLM”.")
            return
        review = result.review
        color = "#1a7f37" if result.ok else "#d73027"
        if review is not None and review.warnings and result.ok:
            color = "#bf8700"
        text = f"<span style='color:{color}'><b>{_esc(result.summary())}</b></span>"
        if stale or self.stale:
            text += " <i>The query changed since this suggestion; ask again before applying it.</i>"
        elif result.review is not None and not result.review.ok:
            text += " This rewrite cannot be applied: it failed validation."
        self.status.setText(text)

    def _show_exchange(self) -> None:
        if self._result is None:
            return
        text = ("=== PROMPT ===\n" + self._result.prompt + "\n\n=== RAW ANSWER ===\n"
                + (self._result.raw_response or self._result.error))
        _TextDialog("Local LLM exchange", text, self).exec()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
