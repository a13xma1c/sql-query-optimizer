"""End-to-end GUI scenarios (offscreen Qt): the user journeys, with real database round trips.

Dialogs that need a human (file pickers, message boxes, paste dialog) are replaced by stubs that return what the
user would have chosen; everything else - workers, plan fetching, analysis, rendering - is the real thing.
"""

import json

import pytest
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QMessageBox

from sqlanalyzer.dialects import ENGINES, Engine
from sqlanalyzer.plans.graph import graphviz_available

from .conftest import CLEAN_SQL, DATA, MESSY_SQL, SCHEMA_TEXT, EngineEnv, _recorded_config

pytestmark = [pytest.mark.e2e, pytest.mark.gui]
pytest.importorskip("pytestqt")


# ------------------------------------------------------------------- fixtures
def settle(window, qtbot, timeout: int = 20_000) -> None:
    """Wait for every background worker the window may have started."""
    qtbot.waitUntil(lambda: not window.optimizing and not window.explaining, timeout=timeout)


@pytest.fixture
def app(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("SQLANALYZER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("SQLANALYZER_HISTORY_FILE", str(tmp_path / "history.json"))
    from sqlanalyzer.gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    settle(window, qtbot)
    yield window
    settle(window, qtbot)
    qtbot.waitUntil(lambda: window.compare.run_btn.isEnabled(), timeout=20_000)


@pytest.fixture
def duck_app(app, duckdb_cfg, qtbot):
    """The window connected to the live DuckDB sample database."""
    app.engine_box.setCurrentIndex(app.engine_box.findData(Engine.DUCKDB))
    app.session.save_connection(duckdb_cfg)
    app._refresh_connection_label()
    app.editor.setPlainText(MESSY_SQL)
    app.analyze()
    settle(app, qtbot)
    return app


class _Messages:
    """Records QMessageBox calls and answers them like a user would."""

    def __init__(self, monkeypatch, answer=QMessageBox.StandardButton.Yes):
        self.calls: list[tuple[str, str, str]] = []
        self.answer = answer
        for kind in ("critical", "warning", "information"):
            monkeypatch.setattr(QMessageBox, kind, self._make(kind))
        monkeypatch.setattr(QMessageBox, "question", self._make("question"))

    def _make(self, kind):
        def fn(parent, title, text, *args, **kwargs):
            self.calls.append((kind, str(title), str(text)))
            return self.answer if kind == "question" else QMessageBox.StandardButton.Ok

        return staticmethod(fn)

    def of(self, kind: str) -> list[str]:
        return [text for k, _, text in self.calls if k == kind]


def explain(window, qtbot, analyze: bool = False):
    window.explain(analyze)
    settle(window, qtbot)


# ----------------------------------------------------------- the main journey
def test_journey_from_messy_query_to_validated_rewrite(duck_app, qtbot, monkeypatch):
    w = duck_app
    messages = _Messages(monkeypatch)
    assert "no connection" not in w.connection_label.text() and "duckdb" in w.connection_label.text()
    findings_before = {f.rule for f in w.analysis.findings}
    assert {"implicit-join", "non-sargable-predicate", "not-in-subquery"} <= findings_before
    assert "Offline plan" in w.plan.source_label.text()

    # 1. Explain on the real database
    explain(w, qtbot)
    assert w.analysis.plan is not None and w.analysis.plan.engine is Engine.DUCKDB
    assert w.tabs.currentWidget() is w.plan and "loaded from the engine" in w.plan.source_label.text()
    assert w.plan.tree.topLevelItemCount() == 1 and w.plan.tree.topLevelItem(0).childCount() >= 1
    assert "DuckDB plan" in w.plan.explanation_view.toPlainText()
    assert "rows estimated" in w.plan.explanation_view.toPlainText()
    if graphviz_available():
        assert w.plan.view.svg_bytes.startswith(b"<?xml")
    entry = w.session.history.entries()[0]  # the explain was recorded with its outcome
    assert entry.source == "explain" and entry.plan_source == "explain" and entry.findings == len(w.analysis.findings)
    assert w.analysis.index_recommendations and w.tuning.table.rowCount() >= 1

    # 2. Keep the messy version as A, take the rule engine's rewrite, keep it as B
    w._to_compare(old=True)
    w.tabs.setCurrentWidget(w.optimize)
    assert w.optimize.table.rowCount() >= 3
    rewritten = w.analysis.optimization.optimized_sql
    w.optimize._apply()
    settle(w, qtbot)
    assert w.editor.toPlainText() == rewritten
    assert not w.analysis.optimization.changed  # nothing left to rewrite
    assert not ({"implicit-join", "non-sargable-predicate", "not-in-subquery"} & {f.rule for f in w.analysis.findings})
    w._to_compare(old=False)

    # 3. Explain the rewrite: a different real plan
    explain(w, qtbot)
    assert w.analysis.plan is not None and w.plan_text
    assert messages.of("critical") == []

    # 4. Compare both versions with their engine plans
    w.tabs.setCurrentWidget(w.compare)
    w.compare.run()
    qtbot.waitUntil(lambda: w.compare.run_btn.isEnabled() and "compared" in w.compare.status.text().lower(),
                    timeout=20_000)
    assert w.compare.status.text() == "Plans compared."
    summary = w.compare.summary.toPlainText()
    assert "resolved" in summary and "Operators:" in summary and "Offline cost model" in summary
    entries = w.session.history.entries()
    assert len(entries) == 2 and all(e.plan_source == "explain" for e in entries)  # both versions were explained
    assert entries[0].runs == 2  # recorded by the rewrite, then refreshed by its explain
    assert w.compare.diff_tree.topLevelItemCount() >= 1


def test_explain_analyze_needs_confirmation_and_refuses_writes(duck_app, qtbot, monkeypatch):
    w = duck_app
    no = _Messages(monkeypatch, answer=QMessageBox.StandardButton.No)
    explain(w, qtbot, analyze=True)
    assert w.analysis.plan is None and no.of("question")  # declined: nothing ran
    yes = _Messages(monkeypatch, answer=QMessageBox.StandardButton.Yes)
    explain(w, qtbot, analyze=True)
    assert w.analysis.plan is not None and w.analysis.plan.analyzed
    assert "EXPLAIN ANALYZE" in w.plan.source_label.text()
    assert any(n.actual_rows is not None for n in w.analysis.plan.nodes())
    assert "rows actual" in w.plan.explanation_view.toPlainText()
    # a write statement is refused before it reaches the database
    w.editor.setPlainText("DELETE FROM orders WHERE id = 1")
    w.analyze()
    settle(w, qtbot)
    explain(w, qtbot, analyze=True)
    assert any("Only plain SELECT" in text for text in yes.of("critical"))


def test_explain_failure_is_reported_and_plan_stays_offline(duck_app, qtbot, monkeypatch):
    w = duck_app
    messages = _Messages(monkeypatch)
    w.editor.setPlainText("SELECT * FROM missing_table")
    w.analyze()
    settle(w, qtbot)
    explain(w, qtbot)
    assert any("missing_table" in text.lower() for text in messages.of("critical"))
    assert w.analysis.plan is None and w.analysis.effective_plan is w.analysis.logical_plan


def test_findings_navigate_to_plan_nodes(duck_app, qtbot, monkeypatch):
    w = duck_app
    _Messages(monkeypatch)
    explain(w, qtbot, analyze=True)
    node_findings = [f for f in w.analysis.findings if f.node_id is not None]
    if not node_findings:
        pytest.skip("the DuckDB plan produced no node-level findings on this machine")
    row = next(i for i, f in enumerate(w.findings._shown) if f.node_id is not None)
    w.findings._activate(row, 0)
    assert w.tabs.currentWidget() is w.plan
    assert w.plan.tree.currentItem() is not None
    assert w.plan.detail.toPlainText()


# ------------------------------------------------------------- other engines
@pytest.mark.parametrize("engine", [Engine.TRINO, Engine.SNOWFLAKE], ids=["trino", "snowflake"])
def test_explain_on_recorded_engines(app, qtbot, recorded, engine, monkeypatch):
    w = app
    messages = _Messages(monkeypatch)
    server = recorded(engine)
    w.engine_box.setCurrentIndex(w.engine_box.findData(engine))
    assert w.session.engine is engine
    w.session.save_connection(_recorded_config(engine))
    w._refresh_connection_label()
    assert ENGINES[engine].label in w.connection_label.text()
    w.editor.setPlainText(MESSY_SQL)
    w.analyze()
    settle(w, qtbot)
    explain(w, qtbot)
    assert messages.of("critical") == []
    assert server.statements[-1].startswith(EngineEnv(engine, _recorded_config(engine), False).explain_prefix())
    plan = w.analysis.plan
    assert plan is not None and plan.engine is engine
    ops = [n.op for n in plan.nodes()]
    assert ops[0] == {"trino": "Output", "snowflake": "Result"}[engine.value]
    assert ENGINES[engine].label in w.plan.explanation_view.toPlainText()
    if engine is Engine.TRINO:
        assert "Moves rows between workers" in w.plan.explanation_view.toPlainText()
    assert any(f.node_id is not None for f in w.analysis.findings)  # partition findings from the fixtures
    # switching the engine drops the plan and falls back to the offline one
    w.engine_box.setCurrentIndex(w.engine_box.findData(Engine.DUCKDB))
    settle(w, qtbot)
    assert w.plan_text == "" and w.analysis.plan is None and "Offline plan" in w.plan.source_label.text()


def test_explain_on_live_postgres(app, qtbot, pg_server, monkeypatch):
    if pg_server is None:
        pytest.skip("no PostgreSQL available")
    w = app
    _Messages(monkeypatch)
    w.engine_box.setCurrentIndex(w.engine_box.findData(Engine.POSTGRES))
    w.session.save_connection(pg_server)
    w.editor.setPlainText(MESSY_SQL)
    w.analyze()
    settle(w, qtbot)
    explain(w, qtbot, analyze=True)
    plan = w.analysis.plan
    assert plan is not None and plan.engine is Engine.POSTGRES and plan.analyzed
    assert any(n.op == "Seq Scan" and n.relation == "orders" for n in plan.nodes())
    assert {"scan-discards-rows", "function-in-scan-filter"} <= {f.rule for f in w.analysis.findings}
    assert "PostgreSQL plan with measured" in w.plan.explanation_view.toPlainText()
    # the rewrite gets a better real plan
    w.optimize._apply()
    settle(w, qtbot)
    explain(w, qtbot, analyze=True)
    assert "scan-discards-rows" not in {f.rule for f in w.analysis.findings}
    assert any("Index" in n.op for n in w.analysis.plan.nodes())


# ------------------------------------------------------------------ dialogs
def test_connection_dialog_round_trip(app, qtbot, duckdb_cfg, pg_server, tmp_path):
    from sqlanalyzer.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog(Engine.DUCKDB, duckdb_cfg, app)
    qtbot.addWidget(dialog)
    assert dialog.database.text() == duckdb_cfg.database
    dialog._test()
    qtbot.waitUntil(lambda: "successful" in dialog.status.text(), timeout=20_000)
    app.session.save_connection(dialog.config())
    assert app.session.connections[Engine.DUCKDB].database == duckdb_cfg.database
    stored = json.loads(app.session.settings.path.read_text())
    assert stored["connection/duckdb/database"] == duckdb_cfg.database

    broken = ConnectionDialog(Engine.DUCKDB, None, app)
    qtbot.addWidget(broken)
    broken.database.setText(str(tmp_path / "no" / "such" / "dir" / "x.duckdb"))
    broken._test()
    qtbot.waitUntil(lambda: broken.status.text() not in ("", "Connecting…"), timeout=20_000)
    assert "successful" not in broken.status.text()

    if pg_server is not None:
        pg_dialog = ConnectionDialog(Engine.POSTGRES, pg_server, app)
        qtbot.addWidget(pg_dialog)
        assert pg_dialog.host.text() == "127.0.0.1" and pg_dialog.port.value() == pg_server.port
        pg_dialog.password.setText("not-persisted")
        pg_dialog._test()
        qtbot.waitUntil(lambda: "successful" in pg_dialog.status.text(), timeout=20_000)
        app.session.save_connection(pg_dialog.config())
        stored = json.loads(app.session.settings.path.read_text())
        assert stored["connection/postgres/host"] == "127.0.0.1"
        assert "password" not in json.dumps(stored) and "not-persisted" not in json.dumps(stored)


def test_paste_and_open_plan_files(app, qtbot, monkeypatch):
    w = app
    messages = _Messages(monkeypatch)
    w.engine_box.setCurrentIndex(w.engine_box.findData(Engine.POSTGRES))
    w.editor.setPlainText("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id")
    w.analyze()
    settle(w, qtbot)

    class StubPaste:  # what the user pastes into the dialog
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return True

        def plan_text(self):
            return (DATA / "postgres_plan.json").read_text()

    monkeypatch.setattr("sqlanalyzer.gui.main_window.PastePlanDialog", StubPaste)
    w.paste_plan()
    assert w.analysis.plan is not None and w.analysis.plan.analyzed
    assert w.tabs.currentWidget() is w.plan

    monkeypatch.setattr("sqlanalyzer.gui.plan_tab.QFileDialog.getOpenFileName",
                        lambda *a, **k: (str(DATA / "postgres_nested_loop.json"), ""))
    w.plan._open_file()
    assert "cartesian-product" in {f.rule for f in w.analysis.findings}

    StubPaste.plan_text = lambda self: "this is not a plan"
    w.paste_plan()
    assert any("Plan not recognized" == title for _, title, _ in messages.calls)
    assert w.plan_text == "" and w.analysis.plan is None


def test_open_save_sql_and_exports(app, qtbot, tmp_path, monkeypatch):
    w = app
    sql_path = tmp_path / "query.sql"
    monkeypatch.setattr("sqlanalyzer.gui.main_window.QFileDialog.getSaveFileName", lambda *a, **k: (str(sql_path), ""))
    w.editor.setPlainText(CLEAN_SQL)
    w.save_file()
    assert sql_path.read_text() == CLEAN_SQL
    w.editor.setPlainText("SELECT 1")
    monkeypatch.setattr("sqlanalyzer.gui.main_window.QFileDialog.getOpenFileName", lambda *a, **k: (str(sql_path), ""))
    w.open_file()
    assert w.editor.toPlainText() == CLEAN_SQL
    w.analyze()
    settle(w, qtbot)

    svg_path = tmp_path / "plan.svg"
    monkeypatch.setattr("sqlanalyzer.gui.plan_tab.QFileDialog.getSaveFileName", lambda *a, **k: (str(svg_path), ""))
    w.plan._export("svg")
    if graphviz_available():
        assert svg_path.read_bytes().startswith(b"<?xml")
    else:
        assert not svg_path.exists()

    ast_path = tmp_path / "ast.json"
    monkeypatch.setattr("sqlanalyzer.gui.ast_tab.QFileDialog.getSaveFileName", lambda *a, **k: (str(ast_path), ""))
    w.tabs.setCurrentWidget(w.ast)
    qtbot.waitUntil(lambda: w.ast.tree.topLevelItemCount() == 1, timeout=5000)
    w.ast._export()
    assert json.loads(ast_path.read_text())[0]["kind"] == "Select"


def test_format_and_keyboard_actions(app, qtbot):
    w = app
    w.editor.setPlainText("select a,b from t where x=1")
    w.format_query()
    assert w.editor.toPlainText().startswith("SELECT\n  a,")
    actions = {a.text(): a for a in w.findChildren(QAction)}
    for name in ("Analyze", "Format", "Optimize", "Ask local LLM", "Explain", "Explain offline"):
        assert name in actions and actions[name].shortcut().toString()
    actions["Explain offline"].trigger()
    assert w.tabs.currentWidget() is w.plan and w.plan.panes.currentWidget() is w.plan.explanation_view
    actions["Optimize"].trigger()
    assert w.tabs.currentWidget() is w.optimize


# ------------------------------------------------------------------ LLM flow
def test_llm_journey_with_validation_and_engine_explain(duck_app, qtbot, monkeypatch):
    w = duck_app
    _Messages(monkeypatch)
    from sqlanalyzer.llm import LlmClient

    class Stub(LlmClient):
        def complete(self, prompt, system=""):
            assert "customers: id int" in prompt  # the declared schema reaches the model
            return json.dumps({"sql": CLEAN_SQL, "changes": [{"title": "anti join", "risk": "none"}]})

    monkeypatch.setattr("sqlanalyzer.gui.llm_panel.LlmClient", Stub)
    w.schema_edit.setPlainText(SCHEMA_TEXT)
    w.analyze()
    settle(w, qtbot)
    w.ask_llm()
    qtbot.waitUntil(lambda: not w.optimize.llm.busy, timeout=20_000)
    panel = w.optimize.llm
    assert panel.result.ok and ("Validated" in panel.status.text() or "Needs review" in panel.status.text())
    assert w.optimize.pages.currentWidget() is panel and w.optimize.apply_btn.isEnabled()
    w.optimize._apply()
    settle(w, qtbot)
    assert "NOT EXISTS" in w.editor.toPlainText()
    explain(w, qtbot)
    assert w.analysis.plan is not None and w.analysis.plan.engine is Engine.DUCKDB


# --------------------------------------------------------------- persistence
def test_settings_survive_a_restart(app, qtbot, duckdb_cfg):
    from sqlanalyzer.gui.main_window import MainWindow
    from sqlanalyzer.llm import LlmConfig

    app.schema_edit.setPlainText(SCHEMA_TEXT)
    app.hints_edit.setPlainText("orders: ts")
    app.session.save_connection(duckdb_cfg)
    app.session.save_optimizer(False, {"union-all", "unnest-subqueries"})
    app.session.save_llm(LlmConfig(provider="openai", base_url="http://localhost:9999", model="m", api_key="k"))
    settle(app, qtbot)

    again = MainWindow()
    qtbot.addWidget(again)
    settle(again, qtbot)
    assert again.schema_edit.toPlainText() == SCHEMA_TEXT and again.hints_edit.toPlainText() == "orders: ts"
    assert again.session.connections[Engine.DUCKDB].database == duckdb_cfg.database
    assert not again.session.include_review and again.session.disabled_rules == {"union-all", "unnest-subqueries"}
    assert again.session.llm.model == "m" and again.session.llm.provider == "openai"
    assert again.session.llm.api_key == ""  # never persisted
    assert not again.optimize.review_box.isChecked()
