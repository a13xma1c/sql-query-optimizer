"""Main application window."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from sqlglot.errors import SqlglotError

from ..analyzer import QueryAnalysis, analyze_query, run_optimization
from ..db import fetch_plan_text
from ..dialects import ENGINES, Engine
from ..models import Category
from ..parsing import format_sql
from .ast_tab import AstTab
from .compare_tab import CompareTab
from .complexity_tab import ComplexityTab
from .connection_dialog import ConnectionDialog
from .editor import CodeEditor
from .findings_tab import FindingsTab
from .optimize_tab import OptimizeTab
from .paste_dialog import PastePlanDialog
from .plan_tab import PlanTab
from .session import Session
from .workers import run_async

SAMPLE = """-- Try the analyzer: this query has several common performance problems
SELECT *
FROM orders o, customers c
WHERE o.customer_id = c.id
  AND DATE(o.order_date) = '2024-01-15'
  AND c.name LIKE '%smith'
  AND o.id NOT IN (SELECT order_id FROM refunds)
ORDER BY 1
"""


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("SQL Analyzer")
        self.resize(1360, 860)
        self.session = Session()
        self.analysis: QueryAnalysis | None = None
        self.plan_text = ""
        self._optimize_generation = 0
        self.optimizing = False  # an optimization run is in flight (see _optimize_async)

        self.editor = CodeEditor("Type or paste a SQL query…")
        self.editor.setPlainText(SAMPLE)
        self.editor.textChanged.connect(self._schedule)
        self._timer = QTimer(self, singleShot=True, interval=500)
        self._timer.timeout.connect(self.analyze)

        self.findings = FindingsTab()
        self.optimize = OptimizeTab(self.session)
        self.plan = PlanTab()
        self.ast = AstTab()
        self.complexity = ComplexityTab()
        self.compare = CompareTab(self.session)
        self.hints_edit = QPlainTextEdit(self.session.hints_text)
        self.hints_edit.setPlaceholderText("Partition columns, one table per line:\nanalytics.events: event_date\nlogs: dt, region\n*: dt")
        self.hints_edit.textChanged.connect(self._hints_changed)
        hints_box = QWidget()
        hl = QVBoxLayout(hints_box)
        hl.addWidget(QLabel("Declare partition columns so pruning issues can be detected precisely. "
                            "Format: <code>table: col1, col2</code> (use <code>*</code> for all tables)."))
        hl.addWidget(self.hints_edit, 1)
        self.schema_edit = QPlainTextEdit(self.session.schema_text)
        self.schema_edit.setPlaceholderText("Table columns, one table per line (types optional):\n"
                                            "orders: id int, customer_id int, order_date date, status varchar\n"
                                            "customers: id, name, region")
        self.schema_edit.textChanged.connect(self._schema_changed)
        schema_box = QWidget()
        sl = QVBoxLayout(schema_box)
        sl.addWidget(QLabel("Declare table columns so the optimizer can expand <code>SELECT *</code>, resolve "
                            "every column and run predicate/projection pushdown, subquery merging and join "
                            "elimination. Format: <code>schema.table: col type, col type</code>."))
        sl.addWidget(self.schema_edit, 1)
        self.schema_status = QLabel("")
        self.schema_status.setStyleSheet("color: #d73027;")
        sl.addWidget(self.schema_status)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.findings, "Findings")
        self.tabs.addTab(self.optimize, "Optimize")
        self.tabs.addTab(self.plan, "Execution plan")
        self.tabs.addTab(self.ast, "Syntax tree")
        self.tabs.addTab(self.complexity, "Complexity")
        self.tabs.addTab(hints_box, "Partition hints")
        self.tabs.addTab(schema_box, "Schema")
        self.tabs.addTab(self.compare, "Compare versions")

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.editor)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        self._build_toolbar()
        self.status = self.statusBar()
        self.connection_label = QLabel()
        self.status.addPermanentWidget(self.connection_label)

        self.findings.goto_requested.connect(self.editor.goto)
        self.findings.node_requested.connect(self._show_node)
        self.optimize.goto_requested.connect(self.editor.goto)
        self.optimize.apply_requested.connect(self.replace_sql)
        self.ast.goto_requested.connect(self.editor.goto)
        self.session.optimizer_changed.connect(self.analyze)
        self.plan.explain_requested.connect(self.explain)
        self.plan.paste_requested.connect(self.paste_plan)
        self.plan.plan_loaded.connect(self._set_plan_text)
        self.session.engine_changed.connect(self._engine_changed)

        self._refresh_connection_label()
        self.analyze()

    # ------------------------------------------------------------------ toolbar
    def _build_toolbar(self) -> None:
        bar = QToolBar("Main")
        bar.setMovable(False)
        self.addToolBar(bar)

        self.engine_box = QComboBox()
        for e, info in ENGINES.items():
            self.engine_box.addItem(info.label, e)
        self.engine_box.setCurrentIndex(self.engine_box.findData(self.session.engine))
        self.engine_box.currentIndexChanged.connect(lambda: self.session.set_engine(self.engine_box.currentData()))
        bar.addWidget(QLabel(" Engine: "))
        bar.addWidget(self.engine_box)
        bar.addSeparator()

        def action(text: str, slot, shortcut: str | None = None, tip: str = "") -> QAction:
            act = QAction(text, self)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            act.setToolTip(tip or text)
            bar.addAction(act)
            return act

        action("Analyze", self.analyze, "Ctrl+Return", "Validate and analyze (Ctrl+Enter)")
        action("Format", self.format_query, "Ctrl+Shift+F", "Format the SQL (Ctrl+Shift+F)")
        action("Optimize", lambda: self.tabs.setCurrentWidget(self.optimize), "Ctrl+Shift+O",
               "Show the rule-based rewrite of the query (Ctrl+Shift+O)")
        action("Explain", lambda: self.explain(False), "Ctrl+E", "Run EXPLAIN on the connected database (Ctrl+E)")
        bar.addSeparator()
        action("Connection…", self.edit_connection)
        action("Open SQL…", self.open_file, "Ctrl+O")
        action("Save SQL…", self.save_file, "Ctrl+S")
        bar.addSeparator()
        action("Send to Compare as A", lambda: self._to_compare(old=True), tip="Use the editor text as version A")
        action("Send to Compare as B", lambda: self._to_compare(old=False), tip="Use the editor text as version B")

    # ----------------------------------------------------------------- analysis
    def _schedule(self) -> None:
        self._timer.start()

    def analyze(self) -> None:
        self._timer.stop()
        sql = self.editor.toPlainText()
        self.analysis = analyze_query(sql, self.session.engine, self.session.hints, self.plan_text or None,
                                      optimize=False)
        a = self.analysis
        located = [f for f in a.findings if f.line and f.category is not Category.COMPLEXITY]
        self.editor.mark_findings(located)
        self.findings.set_findings(a.findings)
        self.ast.set_trees(a.ast)
        self.complexity.set_report(a.complexity)
        self.plan.set_plan(a.plan, a.findings)
        self._optimize_async(a)
        self.status.showMessage("Syntax OK" if a.syntax_ok else "Syntax error", 4000)
        self.tabs.setTabText(0, f"Findings ({len(a.findings)})" if a.findings else "Findings")
        if not a.syntax_ok:
            self.tabs.setCurrentWidget(self.findings)

    def _optimize_async(self, analysis: QueryAnalysis) -> None:
        """Run the rewrite engine off the GUI thread; stale results (from older analyses) are dropped."""
        self._optimize_generation += 1
        generation = self._optimize_generation
        if not analysis.syntax_ok:
            self.optimizing = False
            self.optimize.set_analysis(analysis)
            self.tabs.setTabText(1, "Optimize")
            return
        self.optimizing = True
        self.optimize.set_pending()
        schema, hints = self.session.schema, self.session.hints
        include_review, disabled = self.session.include_review, set(self.session.disabled_rules)

        def work():
            return run_optimization(analysis, schema=schema, partition_hints=hints, include_review=include_review,
                                    disabled_rules=disabled)

        def done(_result) -> None:
            if generation != self._optimize_generation:
                return
            self.optimizing = False
            self.optimize.set_analysis(analysis)
            self.findings.set_findings(analysis.findings, {f.rule for f in analysis.resolved_by_optimization})
            n_rw = len(analysis.optimization.rewrites) if analysis.optimization else 0
            self.tabs.setTabText(1, f"Optimize ({n_rw})" if n_rw else "Optimize")

        def failed(msg: str) -> None:
            if generation != self._optimize_generation:
                return
            self.optimizing = False
            self.optimize.set_analysis(analysis)
            self.status.showMessage(f"Optimizer failed: {msg}", 6000)

        run_async(self, work, done, failed)

    def _hints_changed(self) -> None:
        self.session.save_hints(self.hints_edit.toPlainText())
        self._schedule()

    def _schema_changed(self) -> None:
        self.session.save_schema(self.schema_edit.toPlainText())
        errors = self.session.schema_hints.errors
        self.schema_status.setText("; ".join(errors[:3]))
        self._schedule()

    def replace_sql(self, text: str) -> None:
        """Replace the editor contents keeping the undo history."""
        if text != self.editor.toPlainText():
            cursor = self.editor.textCursor()
            cursor.select(cursor.SelectionType.Document)
            cursor.insertText(text)
        self.analyze()

    def _engine_changed(self, _engine: Engine) -> None:
        self.plan_text = ""
        self._refresh_connection_label()
        self.analyze()

    def format_query(self) -> None:
        try:
            text = format_sql(self.editor.toPlainText(), ENGINES[self.session.engine].sqlglot_dialect)
        except SqlglotError as exc:
            QMessageBox.warning(self, "Cannot format", f"Fix the syntax errors first:\n\n{exc}")
            return
        if text != self.editor.toPlainText():
            cursor = self.editor.textCursor()
            cursor.select(cursor.SelectionType.Document)  # keep undo history
            cursor.insertText(text)

    # --------------------------------------------------------------------- plans
    def explain(self, analyze: bool = False) -> None:
        cfg = self.session.connection
        if cfg is None:
            self.edit_connection()
            cfg = self.session.connection
            if cfg is None:
                return
        sql = self.editor.toPlainText()
        if self.analysis is None or not self.analysis.syntax_ok:
            QMessageBox.warning(self, "Cannot explain", "Fix the syntax errors first.")
            return
        if analyze:
            ok = QMessageBox.question(
                self, "Execute query?",
                "EXPLAIN ANALYZE will actually execute this query on the database. Continue?")
            if ok != QMessageBox.StandardButton.Yes:
                return
        self.status.showMessage("Running EXPLAIN…")
        self.tabs.setCurrentWidget(self.plan)
        run_async(self, lambda: fetch_plan_text(cfg, sql, analyze), self._set_plan_text, self._explain_failed)

    def _explain_failed(self, msg: str) -> None:
        self.status.clearMessage()
        QMessageBox.critical(self, "EXPLAIN failed", msg)

    def _set_plan_text(self, text: str) -> None:
        self.plan_text = text
        self.analyze()
        if self.analysis and self.analysis.plan is None:
            msg = next((f.message for f in self.analysis.findings if f.rule == "plan-parse-error"), "Unknown plan format.")
            self.plan_text = ""
            QMessageBox.warning(self, "Plan not recognized", msg)
            self.analyze()
        else:
            self.tabs.setCurrentWidget(self.plan)
            self.status.showMessage("Plan loaded", 4000)

    def paste_plan(self) -> None:
        dlg = PastePlanDialog(self.session.engine, self.plan_text, self)
        if dlg.exec() and dlg.plan_text():
            self._set_plan_text(dlg.plan_text())

    def _show_node(self, node_id: int) -> None:
        self.tabs.setCurrentWidget(self.plan)
        self.plan.select_node(node_id)

    # ----------------------------------------------------------------- misc UI
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._optimize_generation += 1  # ignore results of optimizations still in flight
        self.optimizing = False
        super().closeEvent(event)

    def edit_connection(self) -> None:
        dlg = ConnectionDialog(self.session.engine, self.session.connection, self)
        if dlg.exec():
            self.session.save_connection(dlg.config())
            self._refresh_connection_label()

    def _refresh_connection_label(self) -> None:
        cfg = self.session.connection
        self.connection_label.setText(f"{ENGINES[self.session.engine].label}: "
                                      + (cfg.display() if cfg else "no connection (static analysis + pasted plans)"))

    def _to_compare(self, old: bool) -> None:
        side = self.compare.old if old else self.compare.new
        side.editor.setPlainText(self.editor.toPlainText())
        self.tabs.setCurrentWidget(self.compare)

    def open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open SQL", "", "SQL (*.sql *.txt);;All files (*)")
        if path:
            with open(path, encoding="utf-8") as fh:
                self.editor.setPlainText(fh.read())

    def save_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save SQL", "query.sql", "SQL (*.sql)")
        if path:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(self.editor.toPlainText())
