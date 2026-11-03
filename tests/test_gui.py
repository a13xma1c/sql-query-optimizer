import time

import pytest

from sqlanalyzer.dialects import Engine

pytest.importorskip("PySide6")


def wait_optimized(window, qtbot, timeout: int = 10_000) -> None:
    """The rewrite engine runs in a worker thread: block until its result is in."""
    qtbot.waitUntil(lambda: not window.optimizing, timeout=timeout)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings

    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path))
    from sqlanalyzer.gui.main_window import MainWindow

    w = MainWindow()
    qtbot.addWidget(w)
    w.show()
    wait_optimized(w, qtbot)
    yield w
    wait_optimized(w, qtbot)  # never tear the window down under a running worker


def test_sample_query_is_analyzed(window):
    assert window.analysis.syntax_ok
    rules = {f.rule for f in window.analysis.findings}
    assert {"select-star", "implicit-join", "non-sargable-predicate", "not-in-subquery"} <= rules
    assert window.findings.table.rowCount() == len(window.analysis.findings)
    assert window.complexity.table.rowCount() > 0
    # findings the optimizer resolves are flagged
    assert "resolved by the Optimize tab" in window.findings.summary.text()
    fixed = [window.findings.table.item(r, 4).text() for r in range(window.findings.table.rowCount())]
    assert "✓" in fixed


def test_syntax_error_marks_editor(window):
    window.editor.setPlainText("SELECT FROM WHERE ((")
    window.analyze()
    assert not window.analysis.syntax_ok
    assert len(window.editor.extraSelections()) > 1


def test_format_button(window):
    window.editor.setPlainText("select a,b from t where x=1")
    window.format_query()
    assert window.editor.toPlainText() == "SELECT\n  a,\n  b\nFROM t\nWHERE\n  x = 1;"


def test_engine_switch_and_pasted_plan(window, qtbot):
    from conftest import read

    window.engine_box.setCurrentIndex(window.engine_box.findData(Engine.POSTGRES))
    assert window.session.engine is Engine.POSTGRES
    window.editor.setPlainText("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id")
    window._set_plan_text(read("postgres_plan.json"))
    assert window.analysis.plan is not None
    assert window.plan.tree.topLevelItemCount() == 1
    assert any(f.node_id is not None for f in window.analysis.findings)
    window.plan.select_node(1)
    assert "Seq Scan" in window.plan.detail.toPlainText()


def test_compare_tab_static(window, qtbot):
    window.compare.use_queries("SELECT * FROM a, b WHERE a.x = 1", "SELECT a.x FROM a JOIN b ON a.id = b.id WHERE a.x = 1")
    window.compare.run()
    deadline = time.time() + 5
    while "resolved" not in window.compare.summary.toPlainText() and time.time() < deadline:
        qtbot.wait(20)
    assert "resolved" in window.compare.summary.toPlainText()


# ------------------------------------------------------------------ optimize tab
def test_stale_optimization_results_are_dropped(window, qtbot):
    window.editor.setPlainText("SELECT a FROM t WHERE a = 1 OR a = 2")
    window.analyze()
    window.editor.setPlainText("SELECT b FROM u WHERE b = NULL")
    window.analyze()  # supersedes the first run before it reports back
    wait_optimized(window, qtbot)
    assert [rw.rule for rw in window.analysis.optimization.rewrites] == ["null-comparison"]
    assert window.optimize.table.item(0, 0).text() == "null-comparison"


def test_optimize_tab_lists_rewrites_and_applies_them(window, qtbot):
    tab = window.optimize
    assert tab.table.rowCount() == len(window.analysis.optimization.rewrites) >= 3
    assert window.tabs.tabText(1).startswith("Optimize (")
    assert "Resolves" in tab.summary.text() and "schema-dependent" in tab.summary.text()
    assert tab.output.toPlainText().startswith("/*") or "JOIN customers" in tab.output.toPlainText()
    tab.table.selectRow(0)
    assert "Before:" in tab.detail.toPlainText() and "After:" in tab.detail.toPlainText()
    optimized = window.analysis.optimization.optimized_sql
    tab._apply()
    assert window.editor.toPlainText() == optimized
    wait_optimized(window, qtbot)
    # applying the rewrite leaves nothing more to do
    assert window.analysis.optimization is not None and not window.analysis.optimization.changed
    assert window.tabs.tabText(1) == "Optimize"
    window.editor.undo()
    assert "orders o, customers c" in window.editor.toPlainText()


def test_optimize_tab_review_toggle_and_rules_dialog(window, qtbot):
    from sqlanalyzer.gui.optimize_tab import RulesDialog
    from sqlanalyzer.optimizer import Safety

    window.optimize.review_box.setChecked(False)
    wait_optimized(window, qtbot)
    assert not window.session.include_review
    assert all(rw.safety is Safety.SAFE for rw in window.analysis.optimization.rewrites)
    window.optimize.review_box.setChecked(True)
    wait_optimized(window, qtbot)
    assert any(rw.safety is Safety.REVIEW for rw in window.analysis.optimization.rewrites)

    dlg = RulesDialog(window.session.disabled_rules, window)
    assert not dlg._boxes["unnest-subqueries"].isChecked() and dlg._boxes["explicit-join"].isChecked()
    dlg._boxes["explicit-join"].setChecked(False)
    window.session.save_optimizer(True, dlg.disabled_rules())
    wait_optimized(window, qtbot)
    assert "explicit-join" in window.session.disabled_rules
    assert "explicit-join" not in {rw.rule for rw in window.analysis.optimization.rewrites}
    assert window.analysis.optimization.skipped["explicit-join"] == "disabled"


def test_optimize_tab_double_click_goes_to_source(window, qtbot):
    tab = window.optimize
    row = next(i for i, rw in enumerate(tab._shown) if rw.line)
    with qtbot.waitSignal(tab.goto_requested, timeout=1000) as blocker:
        tab._activate(row, 0)
    assert blocker.args[0] == tab._shown[row].line
    assert window.editor.textCursor().blockNumber() + 1 == tab._shown[row].line


def test_schema_tab_enables_schema_rules(window, qtbot):
    window.schema_edit.setPlainText("orders: id int, customer_id int, order_date date, name text\n"
                                    "customers: id int, name text\nrefunds: order_id int")
    window.analyze()
    wait_optimized(window, qtbot)
    assert window.session.schema is not None and window.schema_status.text() == ""
    opt = window.analysis.optimization
    assert opt.qualified and "qualify" in {rw.rule for rw in opt.rewrites}
    assert "resolved against the schema" in window.optimize.summary.text()
    window.schema_edit.setPlainText("orders: id int\nsales.customers: id int")
    window.analyze()
    wait_optimized(window, qtbot)
    assert "same qualification depth" in window.schema_status.text()
    assert window.session.schema is None and not window.analysis.optimization.qualified


def test_optimize_tab_with_syntax_error(window, qtbot):
    window.editor.setPlainText("SELECT FROM WHERE ((")
    window.analyze()
    wait_optimized(window, qtbot)
    assert window.optimize.table.rowCount() == 0 and not window.optimize.apply_btn.isEnabled()
    assert "once the SQL parses" in window.optimize.summary.text()


# ----------------------------------------------------------------------- AST tab
def test_ast_tab_renders_tree_lazily(window, qtbot):
    tab = window.ast
    assert tab._dirty and tab.tree.topLevelItemCount() == 0  # not visible yet: deferred
    window.tabs.setCurrentWidget(tab)
    qtbot.waitUntil(lambda: tab.tree.topLevelItemCount() == 1, timeout=2000)
    assert not tab._dirty
    root = tab.tree.topLevelItem(0)
    assert root.text(0) == "Select" and root.childCount() == len(window.analysis.ast[0].children)
    assert "nodes" in tab.stats.text() and "depth" in tab.stats.text()
    # editing the SQL refreshes the tree while visible
    window.editor.setPlainText("SELECT 1")
    window.analyze()
    assert tab.tree.topLevelItem(0).childCount() == 1


def test_ast_tab_filter_and_navigation(window, qtbot):
    window.tabs.setCurrentWidget(window.ast)
    tab = window.ast
    qtbot.waitUntil(lambda: tab.tree.topLevelItemCount() == 1, timeout=2000)
    tab.filter.setText("join")
    visible = [item for item in tab._items.values() if not item.isHidden()]
    assert any(item.text(0).startswith("Join") for item in visible)
    assert all(not (item.text(0).startswith("Literal") and not item.isHidden()) or item.childCount() for item in visible)
    tab.filter.setText("")
    assert all(not item.isHidden() for item in tab._items.values())
    item = next(i for (stmt, nid), i in tab._items.items() if i.text(0) == "Identifier customers")
    with qtbot.waitSignal(tab.goto_requested, timeout=1000) as blocker:
        tab._activated(item, 0)
    assert blocker.args == [3, 16]  # "customers" on line 3 of the sample


def test_ast_tab_graph_and_export(window, qtbot, tmp_path, monkeypatch):
    from sqlanalyzer.ast import graphviz_available

    window.tabs.setCurrentWidget(window.ast)
    tab = window.ast
    qtbot.waitUntil(lambda: tab.tree.topLevelItemCount() == 1, timeout=2000)
    tab.graph_box.setChecked(True)
    if graphviz_available():
        assert tab.view.svg_bytes.startswith(b"<?xml")
    else:
        assert tab.view.svg_bytes == b""
    path = tmp_path / "ast.json"
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getSaveFileName", lambda *a, **k: (str(path), ""))
    tab._export()
    import json

    data = json.loads(path.read_text())
    assert data[0]["kind"] == "Select"
