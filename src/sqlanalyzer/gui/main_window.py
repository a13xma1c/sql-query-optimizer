"""Main application window."""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from sqlglot.errors import SqlglotError

from ..analyzer import QueryAnalysis, analyze_query, run_optimization
from ..db import fetch_plan_text
from ..dialects import ENGINES, Engine
from ..models import Category
from ..optimizer import format_schema, parse_schema
from ..parsing import format_sql
from ..samples import load_samples
from .ast_tab import AstTab
from .compare_tab import CompareTab
from .complexity_tab import ComplexityTab
from .connection_dialog import ConnectionDialog
from .editor import CodeEditor
from .findings_tab import FindingsTab
from .geometry import place_window, save_window
from .history_tab import HistoryTab
from .optimize_tab import OptimizeTab
from .paste_dialog import PastePlanDialog
from .plan_tab import PlanTab
from .query_tabs import QueryTabs, title_from_sql
from .rules_dialog import FindingRulesDialog
from .schema_import_dialog import SchemaImportDialog
from .session import Session
from .theme import Theme, apply_theme
from .tuning_tab import TuningTab
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
        self.session = Session()
        self.analysis: QueryAnalysis | None = None
        self._optimize_generation = 0
        self.optimizing = False  # an optimization run is in flight (see _optimize_async)
        self.explaining = False  # an EXPLAIN round trip is in flight

        self.query_tabs = QueryTabs(schema=lambda: self.session.schema_hints)
        self.query_tabs.new_tab(SAMPLE, "Query 1")
        self.query_tabs.textChanged.connect(self._schedule)
        self.query_tabs.tabSwitched.connect(self._tab_switched)
        self._timer = QTimer(self, singleShot=True, interval=500)
        self._timer.timeout.connect(self.analyze)

        self.findings = FindingsTab()
        self.optimize = OptimizeTab(self.session)
        self.plan = PlanTab()
        self.ast = AstTab()
        self.complexity = ComplexityTab()
        self.tuning = TuningTab()
        self.history = HistoryTab(self.session.history)
        self.compare = CompareTab(self.session)
        self.hints_edit = QPlainTextEdit(self.session.hints_text)
        self.hints_edit.setPlaceholderText("Partition columns, one table per line:\nanalytics.events: event_date\nlogs: dt, region\n*: dt")
        self.hints_edit.textChanged.connect(self._hints_changed)
        hints_box = QWidget()
        hl = QVBoxLayout(hints_box)
        hl.addWidget(_note("Declare partition columns so pruning issues can be detected precisely. "
                           "Format: <code>table: col1, col2</code> (use <code>*</code> for all tables)."))
        hl.addWidget(self.hints_edit, 1)
        self.schema_edit = QPlainTextEdit(self.session.schema_text)
        self.schema_edit.setPlaceholderText(
            "Table columns, one table per line (types and statistics optional):\n"
            "orders (rows=2000000): id int pk, customer_id int indexed, ts timestamp, status varchar ndv=4\n"
            "customers (rows=50000): id int pk, name, region")
        self.schema_edit.textChanged.connect(self._schema_changed)
        schema_box = QWidget()
        sl = QVBoxLayout(schema_box)
        sl.addWidget(_note("Declare table columns so the optimizer can expand <code>SELECT *</code>, resolve "
                           "every column and run predicate/projection pushdown, subquery merging and join "
                           "elimination. Row counts, keys and indexes feed the cost model and the index advisor. "
                           "Format: <code>schema.table (rows=N): col type [pk|unique] [indexed] [ndv=N], ...</code>."))
        sl.addWidget(self.schema_edit, 1)
        self.schema_status = QLabel("")
        self.schema_status.setStyleSheet("color: #d73027;")
        schema_bar = QHBoxLayout()
        self.import_schema_btn = QPushButton("Import from database…")
        self.import_schema_btn.setToolTip("Read tables, columns, keys, indexes, statistics and partition keys "
                                          "from the connected database")
        self.import_schema_btn.clicked.connect(self.import_schema)
        schema_bar.addWidget(self.import_schema_btn)
        schema_bar.addWidget(self.schema_status, 1)
        sl.addLayout(schema_bar)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.findings, "Findings")
        self.tabs.addTab(self.optimize, "Optimize")
        self.tabs.addTab(self.plan, "Execution plan")
        self.tabs.addTab(self.ast, "Syntax tree")
        self.tabs.addTab(self.tuning, "Tuning")
        self.tabs.addTab(self.complexity, "Complexity")
        self.tabs.addTab(hints_box, "Partition hints")
        self.tabs.addTab(schema_box, "Schema")
        self.tabs.addTab(self.compare, "Compare versions")
        self.tabs.addTab(self.history, "History")
        # The pages' button bars would otherwise dictate a minimum window width of ~760 px; let the tabs be
        # narrower than their hint on small displays (the splitter still gives them all the room there is).
        self.tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self.tabs.setMinimumWidth(480)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.query_tabs)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        self.setCentralWidget(splitter)

        self._build_toolbar()
        self.status = self.statusBar()
        self.connection_label = QLabel()
        self.status.addPermanentWidget(self.connection_label)

        self.findings.goto_requested.connect(lambda line, col: self.editor.goto(line, col))
        self.findings.node_requested.connect(self._show_node)
        self.findings.apply_requested.connect(self.replace_sql)
        self.findings.rules_requested.connect(self.edit_finding_rules)
        self.tuning.node_requested.connect(self._show_node)
        self.history.load_requested.connect(self.load_from_history)
        self.history.compare_requested.connect(self._history_to_compare)
        self.optimize.goto_requested.connect(lambda line, col: self.editor.goto(line, col))
        self.optimize.apply_requested.connect(self.replace_sql)
        self.ast.goto_requested.connect(lambda line, col: self.editor.goto(line, col))
        self.session.optimizer_changed.connect(self.analyze)
        self.plan.explain_requested.connect(self.explain)
        self.plan.paste_requested.connect(self.paste_plan)
        self.plan.plan_loaded.connect(self._set_plan_text)
        self.session.engine_changed.connect(self._engine_changed)

        self._refresh_connection_label()
        if len(self.session.history):
            self.tabs.setTabText(self.tabs.indexOf(self.history), f"History ({len(self.session.history)})")
        self.apply_theme(self.session.theme)
        place_window(self, self.session.settings)
        self.analyze()

    # ------------------------------------------------------------------- theme
    def apply_theme(self, theme: Theme) -> None:
        """Switch the application's light/dark palette and re-derive every open editor's colours."""
        app = QApplication.instance()
        if isinstance(app, QApplication):
            apply_theme(app, theme)
        self.query_tabs.set_theme(theme)
        for editor in self.findChildren(CodeEditor):
            editor.set_theme(theme)
        for action, value in getattr(self, "_theme_actions", {}).items():
            action.setChecked(value is theme)
        self.session.save_theme(theme)

    # --------------------------------------------------------------- active tab
    @property
    def editor(self) -> CodeEditor:
        """The editor of the currently active query tab."""
        return self.query_tabs.current_editor

    @property
    def plan_text(self) -> str:
        """The pasted/fetched EXPLAIN text that belongs to the active tab's query."""
        return self.query_tabs.plan_text

    @plan_text.setter
    def plan_text(self, value: str) -> None:
        self.query_tabs.plan_text = value

    def _tab_switched(self, _index: int) -> None:
        self.analyze()

    def edit_finding_rules(self) -> None:
        dlg = FindingRulesDialog(self.session.disabled_finding_rules, self)
        if dlg.exec():
            self.session.save_finding_rules(dlg.disabled_rules())  # emits optimizer_changed -> self.analyze()

    def new_query_tab(self) -> None:
        self.query_tabs.new_tab()
        self.analyze()

    def close_query_tab(self) -> None:
        self.query_tabs.close_tab(self.query_tabs.currentIndex())

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

        action("Analyze", lambda: self.analyze(record=True), "Ctrl+Return",
               "Validate and analyze; records the query in the history (Ctrl+Enter)")
        action("Format", self.format_query, "Ctrl+Shift+F", "Format the SQL (Ctrl+Shift+F)")
        action("Optimize", lambda: self.tabs.setCurrentWidget(self.optimize), "Ctrl+Shift+O",
               "Show the rule-based rewrite of the query (Ctrl+Shift+O)")
        action("Ask local LLM", self.ask_llm, "Ctrl+L",
               "Let a locally hosted model rewrite the query; the result is validated against the rules (Ctrl+L)")
        action("Explain", lambda: self.explain(False), "Ctrl+E", "Run EXPLAIN on the connected database (Ctrl+E)")
        action("Explain offline", self.show_explanation, "Ctrl+Shift+E",
               "Explain the plan derived from the SQL, without a database (Ctrl+Shift+E)")
        bar.addSeparator()
        action("New tab", self.new_query_tab, "Ctrl+T", "Open a new, empty query tab (Ctrl+T)")
        action("Close tab", self.close_query_tab, "Ctrl+W", "Close the current query tab (Ctrl+W)")
        bar.addSeparator()
        action("Connection…", self.edit_connection)
        action("Open SQL…", self.open_file, "Ctrl+O")
        self.samples_button = QToolButton()
        self.samples_button.setText("Samples")
        self.samples_button.setToolTip("Open one of the bundled sample queries")
        self.samples_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.samples_menu = QMenu(self.samples_button)
        for sample in load_samples():
            act = self.samples_menu.addAction(sample.name)
            act.setToolTip(sample.description)
            act.setData(sample.key)
            act.triggered.connect(lambda _checked=False, key=sample.key: self.open_sample(key))
        self.samples_button.setMenu(self.samples_menu)
        bar.addWidget(self.samples_button)
        action("Save SQL…", self.save_file, "Ctrl+S")
        bar.addSeparator()
        action("Send to Compare as A", lambda: self._to_compare(old=True), tip="Use the editor text as version A")
        action("Send to Compare as B", lambda: self._to_compare(old=False), tip="Use the editor text as version B")
        bar.addSeparator()
        self._build_theme_menu(bar)

    def _build_theme_menu(self, bar: QToolBar) -> None:
        from PySide6.QtGui import QActionGroup

        button = QToolButton()
        button.setText("Theme")
        button.setToolTip("Light/dark colour theme")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(button)
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._theme_actions: dict[QAction, Theme] = {}
        for theme, label in ((Theme.SYSTEM, "System"), (Theme.LIGHT, "Light"), (Theme.DARK, "Dark")):
            act = menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(theme is self.session.theme)
            act.triggered.connect(lambda _checked=False, t=theme: self.apply_theme(t))
            group.addAction(act)
            self._theme_actions[act] = theme
        button.setMenu(menu)
        bar.addWidget(button)

    # ----------------------------------------------------------------- analysis
    def _schedule(self) -> None:
        self._timer.start()

    def analyze(self, record: bool = False, source: str = "analyze") -> None:
        """Re-run every analysis; ``record`` adds the query to the history (explicit user actions only)."""
        self._timer.stop()
        sql = self.editor.toPlainText()
        if self.query_tabs.file_path is None:  # files keep their name; untitled tabs follow the query
            self.query_tabs.retitle_current(title_from_sql(sql))
        self.analysis = analyze_query(sql, self.session.engine, self.session.hints, self.plan_text or None,
                                      schema=self.session.schema, stats=self.session.stats, optimize=False,
                                      disabled_finding_rules=self.session.disabled_finding_rules)
        a = self.analysis
        located = [f for f in a.findings if f.line and f.category is not Category.COMPLEXITY]
        self.editor.mark_findings(located)
        self.findings.set_findings(a.findings)
        self.ast.set_trees(a.ast)
        self.complexity.set_report(a.complexity)
        self.tuning.set_analysis(a)
        self.plan.set_plan(a.effective_plan, a.findings, a.explanation)
        self._optimize_async(a)
        self.status.showMessage("Syntax OK" if a.syntax_ok else "Syntax error", 4000)
        self.tabs.setTabText(0, f"Findings ({len(a.findings)})" if a.findings else "Findings")
        n_idx = len(a.index_recommendations)
        self.tabs.setTabText(self.tabs.indexOf(self.tuning), f"Tuning ({n_idx})" if n_idx else "Tuning")
        if not a.syntax_ok:
            self.tabs.setCurrentWidget(self.findings)
        if record and a.syntax_ok:
            self.record_history(source)

    def record_history(self, source: str = "analyze") -> None:
        """Store the current query and its outcome in the history tab."""
        a = self.analysis
        if a is None or not a.syntax_ok:
            return
        plan_source = "" if a.plan is None else ("analyze" if a.plan.analyzed else "explain")
        self.session.history.record(
            a.sql, a.engine.value, source=source, findings=len(a.findings),
            errors=sum(f.severity.value == "error" for f in a.findings),
            warnings=sum(f.severity.value == "warning" for f in a.findings),
            complexity=a.complexity.score if a.complexity else None,
            cost=a.cost.total_cost if a.cost else None,
            rewrites=len(a.optimization.rewrites) if a.optimization else 0, plan_source=plan_source)
        self.history.refresh()
        self.tabs.setTabText(self.tabs.indexOf(self.history), f"History ({len(self.session.history)})")

    def open_sample(self, key: str) -> None:
        """Load a bundled sample query (and switch to an engine it supports)."""
        from ..samples import get_sample

        sample = get_sample(key)
        if not sample.supports(self.session.engine):
            self.engine_box.setCurrentIndex(self.engine_box.findData(sample.engines[0]))
        self.editor.setPlainText(f"-- {sample.name}\n-- {sample.description}\n{sample.sql}")
        self.analyze()
        self.tabs.setCurrentWidget(self.findings)

    def referenced_tables(self) -> list[str]:
        """Physical table names used by the current query (for scoped schema imports)."""
        from sqlglot import exp

        if self.analysis is None or not self.analysis.syntax_ok:
            return []
        ctes = {c.alias.lower() for s in self.analysis.statements for c in s.find_all(exp.CTE)}
        names = []
        for stmt in self.analysis.statements:
            for table in stmt.find_all(exp.Table):
                name = table.name.lower()
                if name and name not in ctes and name not in names:
                    names.append(name)
        return names

    def import_schema(self) -> None:
        """Introspect the connected database and put the result into the Schema tab."""
        cfg = self.session.connection
        if cfg is None:
            self.edit_connection()
            cfg = self.session.connection
            if cfg is None:
                return
        dialog = SchemaImportDialog(cfg, self.referenced_tables(), self)
        if not dialog.exec() or dialog.imported is None:
            return
        self.apply_imported_schema(dialog.imported.hints, merge=dialog.merge_mode)
        self.status.showMessage(f"Schema imported: {dialog.imported.summary()}", 8000)
        self.tabs.setCurrentWidget(self.findings)

    def apply_imported_schema(self, hints, merge: bool = True) -> None:
        current = parse_schema(self.schema_edit.toPlainText())
        combined = current.merged(hints) if merge and not current.empty else hints
        self.schema_edit.setPlainText(format_schema(combined))  # saves the hints
        self.analyze()

    def load_from_history(self, sql: str, engine: str) -> None:
        if engine in Engine._value2member_map_ and Engine(engine) is not self.session.engine:
            self.engine_box.setCurrentIndex(self.engine_box.findData(Engine(engine)))
        self.editor.setPlainText(sql)
        self.analyze()
        self.tabs.setCurrentWidget(self.findings)

    def _history_to_compare(self, sql: str, as_old: bool) -> None:
        side = self.compare.old if as_old else self.compare.new
        side.editor.setPlainText(sql)
        self.tabs.setCurrentWidget(self.compare)

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
        schema, hints, stats = self.session.schema, self.session.hints, self.session.stats
        include_review, disabled = self.session.include_review, set(self.session.disabled_rules)

        disabled_finding_rules = set(self.session.disabled_finding_rules)

        def work():
            return run_optimization(analysis, schema=schema, stats=stats, partition_hints=hints,
                                    include_review=include_review, disabled_rules=disabled,
                                    disabled_finding_rules=disabled_finding_rules)

        def done(_result) -> None:
            if generation != self._optimize_generation:
                return
            self.optimizing = False
            self.optimize.set_analysis(analysis)
            self.findings.set_findings(analysis.findings, {f.rule for f in analysis.resolved_by_optimization},
                                       analysis.suggestions)
            self.tuning.set_analysis(analysis)
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
        """Replace the editor contents keeping the undo history (and record the rewrite in the history)."""
        if text != self.editor.toPlainText():
            cursor = self.editor.textCursor()
            cursor.select(cursor.SelectionType.Document)
            cursor.insertText(text)
        self.analyze(record=True, source="rewrite")

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
        self.explaining = True
        run_async(self, lambda: fetch_plan_text(cfg, sql, analyze), self._set_plan_text, self._explain_failed)

    def _explain_failed(self, msg: str) -> None:
        self.explaining = False
        self.status.clearMessage()
        QMessageBox.critical(self, "EXPLAIN failed", msg)

    def _set_plan_text(self, text: str) -> None:
        self.explaining = False
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
            self.record_history("explain")

    def paste_plan(self) -> None:
        dlg = PastePlanDialog(self.session.engine, self.plan_text, self)
        if dlg.exec() and dlg.plan_text():
            self._set_plan_text(dlg.plan_text())

    def ask_llm(self) -> None:
        """Switch to the validated local-LLM page and send the current query."""
        self.tabs.setCurrentWidget(self.optimize)
        self.optimize.pages.setCurrentWidget(self.optimize.llm)
        self.optimize.llm.ask()

    def show_explanation(self) -> None:
        """Show the step-by-step plan explanation (offline plan when no EXPLAIN was run)."""
        self.tabs.setCurrentWidget(self.plan)
        self.plan.show_explanation()

    def _show_node(self, node_id: int) -> None:
        self.tabs.setCurrentWidget(self.plan)
        self.plan.select_node(node_id)

    # ----------------------------------------------------------------- misc UI
    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        self._optimize_generation += 1  # ignore results of optimizations still in flight
        self.optimizing = False
        save_window(self, self.session.settings)
        super().closeEvent(event)

    def edit_connection(self) -> None:
        from .profiles import ProfileStore

        profiles = ProfileStore(self.session.settings, self.session.engine)
        dlg = ConnectionDialog(self.session.engine, self.session.connection, self, profiles=profiles)
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
        if not path:
            return
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        # a file opens in its own tab rather than clobbering whatever is already being edited
        self.query_tabs.new_tab(text, os.path.basename(path), file_path=path)
        self.analyze()

    def save_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save SQL", self.query_tabs.file_path or "query.sql", "SQL (*.sql)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.editor.toPlainText())
        self.query_tabs.file_path = path
        self.query_tabs.retitle_current(os.path.basename(path))


def _note(html: str) -> QLabel:
    """A wrapping description label that never dictates the window's minimum width."""
    label = QLabel(html)
    label.setWordWrap(True)
    label.setMinimumWidth(200)
    return label
