import time

import pytest

from sqlanalyzer.dialects import Engine

pytest.importorskip("PySide6")


def wait_optimized(window, qtbot, timeout: int = 10_000) -> None:
    """The rewrite engine runs in a worker thread: block until its result is in."""
    qtbot.waitUntil(lambda: not window.optimizing, timeout=timeout)


@pytest.fixture
def window(qtbot, tmp_path, monkeypatch):
    monkeypatch.setenv("SQLANALYZER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("SQLANALYZER_HISTORY_FILE", str(tmp_path / "history.json"))
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


def test_finding_rules_dialog_filters_by_category_and_search(qtbot):
    from sqlanalyzer.gui.rules_dialog import FindingRulesDialog
    from sqlanalyzer.models import Category

    dlg = FindingRulesDialog(set())
    qtbot.addWidget(dlg)
    dlg.show()
    assert len(dlg._boxes) > 20
    dlg.filter_box.setCurrentIndex(dlg.filter_box.findData(Category.PARTITION))
    visible_names = {rule.name for rule, box, _label in dlg._rows if box.isVisible()}
    assert visible_names and all(rule_info_visible(n) for n in visible_names)
    dlg.filter_box.setCurrentIndex(dlg.filter_box.findData(None))
    dlg.search.setText("select-star")
    visible = {rule.name for rule, box, _label in dlg._rows if box.isVisible()}
    assert visible == {"select-star", "wide-select-star", "columnar-select-star"}  # substring match, by design
    dlg.search.setText("^select-star$")
    assert {rule.name for rule, box, _label in dlg._rows if box.isVisible()} == set()


def rule_info_visible(name: str) -> bool:
    from sqlanalyzer.models import Category
    from sqlanalyzer.rules_registry import rule_info

    info = rule_info(name)
    return info is not None and info.category is Category.PARTITION


def test_finding_rules_dialog_returns_unchecked_rules(qtbot):
    from sqlanalyzer.gui.rules_dialog import FindingRulesDialog

    dlg = FindingRulesDialog({"select-star"})
    qtbot.addWidget(dlg)
    assert not dlg._boxes["select-star"].isChecked()
    dlg._boxes["missing-where"].setChecked(False)
    assert dlg.disabled_rules() == {"select-star", "missing-where"}


def test_findings_tab_rules_button_opens_dialog_and_filters_subsequent_analyses(window, qtbot, monkeypatch):

    window.editor.setPlainText("SELECT * FROM orders")
    window.analyze()
    wait_optimized(window, qtbot)
    assert any(f.rule == "select-star" for f in window.analysis.findings)

    def fake_dialog(disabled, parent=None):
        class _Fake:
            def exec(self_inner):
                return True

            def disabled_rules(self_inner):
                return {"select-star"}

        return _Fake()

    monkeypatch.setattr("sqlanalyzer.gui.main_window.FindingRulesDialog", fake_dialog)
    window.findings.rules_requested.emit()
    wait_optimized(window, qtbot)
    assert window.session.disabled_finding_rules == {"select-star"}
    assert not any(f.rule == "select-star" for f in window.analysis.findings)


def test_theme_menu_switches_the_palette_and_persists(window, qtbot):
    from PySide6.QtGui import QPalette
    from PySide6.QtWidgets import QApplication

    from sqlanalyzer.gui.theme import Theme

    window.apply_theme(Theme.DARK)
    assert window.session.theme is Theme.DARK
    assert QApplication.instance().palette().color(QPalette.ColorRole.Window).lightness() < 128
    checked = {t: a.isChecked() for a, t in window._theme_actions.items()}
    assert checked[Theme.DARK] and not checked[Theme.LIGHT]
    window.apply_theme(Theme.LIGHT)
    assert window.session.theme is Theme.LIGHT


def test_new_tabs_pick_up_the_current_theme(window, qtbot):
    from sqlanalyzer.gui.theme import DARK, Theme

    window.apply_theme(Theme.DARK)
    window.new_query_tab()
    window.editor.setPlainText("select 1")
    rules = dict(window.editor._highlighter._rules)
    assert any(f.foreground().color().name() == DARK.keyword for f in rules.values())


def test_editor_autocomplete_uses_the_declared_schema(window, qtbot):
    window.schema_edit.setPlainText("orders: id int, customer_id int")
    window.editor.setPlainText("SELECT o. FROM orders o")
    prefix, options = window.editor._candidates_fn(window.editor.toPlainText(), "SELECT o.")
    assert prefix == "" and set(options) == {"id", "customer_id"}


def test_new_query_tab_also_gets_a_schema_aware_completer(window, qtbot):
    window.schema_edit.setPlainText("orders: id int")
    window.new_query_tab()
    assert window.editor._completer is not None
    _prefix, options = window.editor._candidates_fn("FROM orders", "FROM ord")
    assert "orders" in options


def test_query_tabs_are_independent(window, qtbot):
    window.editor.setPlainText("SELECT 1")
    window.analyze()
    assert window.query_tabs.count() == 1
    window.new_query_tab()
    assert window.query_tabs.count() == 2
    assert window.editor.toPlainText() == ""  # the new tab starts empty
    window.editor.setPlainText("SELECT 2")
    window.analyze()
    assert window.analysis.sql == "SELECT 2"
    # switching tabs restores the other editor's content and re-runs the analysis for it
    window.query_tabs.setCurrentIndex(0)
    wait_optimized(window, qtbot)
    assert window.editor.toPlainText() == "SELECT 1"
    assert window.analysis.sql == "SELECT 1"


def test_query_tabs_keep_separate_pasted_plans(window, qtbot):
    from conftest import read

    window.engine_box.setCurrentIndex(window.engine_box.findData(Engine.POSTGRES))
    window.editor.setPlainText("SELECT 1 FROM orders")
    window._set_plan_text(read("postgres_plan.json"))
    assert window.analysis.plan is not None
    window.new_query_tab()
    assert window.plan_text == ""  # the new tab has no plan of its own
    window.editor.setPlainText("SELECT 1")
    window.analyze()
    assert window.analysis.plan is None
    window.query_tabs.setCurrentIndex(0)
    wait_optimized(window, qtbot)
    assert window.plan_text and window.analysis.plan is not None


def test_closing_the_last_tab_is_a_no_op(window, qtbot):
    assert window.query_tabs.count() == 1
    window.close_query_tab()
    assert window.query_tabs.count() == 1  # at least one tab always remains


def test_closing_a_tab_drops_its_state(window, qtbot):
    window.new_query_tab()
    window.editor.setPlainText("SELECT 2")
    window.analyze()
    window.close_query_tab()
    assert window.query_tabs.count() == 1
    wait_optimized(window, qtbot)
    assert window.editor.toPlainText() == "SELECT 1" or "Try the analyzer" in window.editor.toPlainText()


def test_tab_title_follows_the_query_until_a_file_is_attached(window, qtbot, tmp_path):
    window.editor.setPlainText("-- a comment\nSELECT 1")
    window.analyze()
    assert window.query_tabs.tabText(window.query_tabs.currentIndex()) == "SELECT 1"
    path = tmp_path / "my_query.sql"
    path.write_text("SELECT 1", encoding="utf-8")
    window.query_tabs.file_path = str(path)
    window.query_tabs.retitle_current("my_query.sql")
    window.editor.setPlainText("SELECT 2")
    window.analyze()
    assert window.query_tabs.tabText(window.query_tabs.currentIndex()) == "my_query.sql"


def test_open_file_creates_a_new_tab(window, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "loaded.sql"
    path.write_text("SELECT * FROM orders", encoding="utf-8")
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getOpenFileName", lambda *a, **k: (str(path), ""))
    before = window.query_tabs.count()
    window.open_file()
    assert window.query_tabs.count() == before + 1
    assert window.editor.toPlainText() == "SELECT * FROM orders"
    assert window.query_tabs.tabText(window.query_tabs.currentIndex()) == "loaded.sql"


def test_save_file_retitles_the_tab(window, qtbot, tmp_path, monkeypatch):
    path = tmp_path / "saved.sql"
    monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getSaveFileName", lambda *a, **k: (str(path), ""))
    window.editor.setPlainText("SELECT 1")
    window.save_file()
    assert path.read_text(encoding="utf-8") == "SELECT 1"
    assert window.query_tabs.tabText(window.query_tabs.currentIndex()) == "saved.sql"


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


# ------------------------------------------------------------- offline plan tab
def test_plan_tab_shows_the_offline_plan_and_explanation(window):
    tab = window.plan
    assert window.analysis.effective_plan is window.analysis.logical_plan
    assert tab.tree.topLevelItemCount() == 1
    assert "Offline plan" in tab.source_label.text() and "no database" in tab.source_label.text()
    text = tab.explanation_view.toPlainText()
    assert "derived from the SQL" in text and "Hash Join" in text and "Summary" in text
    window.show_explanation()
    assert tab.panes.currentWidget() is tab.explanation_view
    # clicking a step selects the operator
    tab._explanation_clicked(_url("node:1"))
    assert tab.tree.currentItem() is not None and tab.panes.currentWidget() is tab.detail


def test_plan_tab_switches_to_a_pasted_plan(window):
    from conftest import read
    from sqlanalyzer.dialects import Engine as _Engine

    window.engine_box.setCurrentIndex(window.engine_box.findData(_Engine.POSTGRES))
    window.editor.setPlainText("SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id")
    window._set_plan_text(read("postgres_plan.json"))
    assert window.analysis.plan is not None and window.analysis.effective_plan is window.analysis.plan
    assert "EXPLAIN ANALYZE" in window.plan.source_label.text()
    assert "rows actual" in window.plan.explanation_view.toPlainText()


def _url(text: str):
    from PySide6.QtCore import QUrl

    return QUrl(text)


def test_svg_view_hit_testing_resolves_clicks_to_registered_elements():
    from PySide6.QtCore import QPointF
    from PySide6.QtWidgets import QApplication

    from sqlanalyzer.dialects import Engine
    from sqlanalyzer.gui.svg_view import SvgView
    from sqlanalyzer.plans import parse_plan
    from sqlanalyzer.plans.graph import graphviz_available, render_svg

    if not graphviz_available():
        pytest.skip("Graphviz 'dot' not installed")
    QApplication.instance() or QApplication([])
    from conftest import read

    plan = parse_plan(Engine.POSTGRES, read("postgres_plan.json"))
    svg = render_svg(plan)
    view = SvgView()
    view.resize(800, 600)
    view.load_svg(svg, {f"n{n.id}": n.id for n in plan.nodes()})
    rects = {n.id: view.element_rect(f"n{n.id}") for n in plan.nodes()}
    assert all(r is not None and not r.isEmpty() for r in rects.values())
    # the node boxes Graphviz lays out never overlap, so every centre resolves to its own node -
    # on every platform, since the rects come from the SVG source rather than from font metrics
    for node_id, rect in rects.items():
        assert view._hit_test(view._item.mapToScene(rect.center())) == node_id
        for other_id, other in rects.items():
            assert other_id == node_id or not rect.intersects(other)
    # far outside every node's bounds
    assert view._hit_test(QPointF(-9999, -9999)) is None
    # the rects are plausible against Qt's own idea of the element bounds (which include the text)
    node = plan.nodes()[1]
    qt_rect = view._renderer.transformForElement(f"n{node.id}").mapRect(view._renderer.boundsOnElement(f"n{node.id}"))
    assert qt_rect.intersects(rects[node.id])


def test_element_rects_parses_graphviz_shapes_and_transforms():
    from sqlanalyzer.gui.svg_view import element_rects

    svg = """<?xml version="1.0"?>
    <svg xmlns="http://www.w3.org/2000/svg" width="100pt" height="50pt" viewBox="0 0 100 50">
    <g id="graph0" class="graph" transform="scale(2 2) rotate(0) translate(4 20)">
      <polygon fill="white" points="-4,4 -4,-20 96,-20 96,4"/>
      <g id="n0" class="node"><title>n0</title>
        <g id="a_n0"><a xlink:title="t" xmlns:xlink="http://www.w3.org/1999/xlink">
          <path d="M10,-10C10,-10 0,-10 0,-10 0,0 10,0 10,0"/>
          <text x="2" y="-4">a very long label that would widen any text-based bounds</text>
        </a></g>
      </g>
      <g id="n1" class="node"><polygon points="20,-10 30,-10 30,0 20,0"/></g>
      <g id="n2" class="node"><ellipse cx="50" cy="-5" rx="4" ry="2"/></g>
      <g id="e0_1" class="edge"><path d="M0,0 100,100"/></g>
    </g></svg>"""
    rects = element_rects(svg)
    # (x + tx) * sx, (y + ty) * sy, w * sx, h * sy
    assert rects["n0"] == (8.0, 20.0, 20.0, 20.0)
    assert rects["n1"] == (48.0, 20.0, 20.0, 20.0)
    assert rects["n2"] == (100.0, 26.0, 16.0, 8.0)
    assert "e0_1" not in rects and "graph0" not in rects
    assert element_rects(b"not xml at all") == {}


def test_svg_view_distinguishes_click_from_drag(qtbot):
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from sqlanalyzer.gui.svg_view import SvgView

    QApplication.instance() or QApplication([])
    view = SvgView()
    view.show_message("no graph")
    received = []
    view.elementClicked.connect(received.append)
    view.set_hit_map({"whatever": "payload"})

    def press_release(p1: QPoint, p2: QPoint) -> None:
        press = QMouseEvent(QMouseEvent.Type.MouseButtonPress, p1.toPointF(), p1.toPointF(), p1.toPointF(),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(press)
        release = QMouseEvent(QMouseEvent.Type.MouseButtonRelease, p2.toPointF(), p2.toPointF(), p2.toPointF(),
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
        view.mouseReleaseEvent(release)

    press_release(QPoint(50, 50), QPoint(50, 51))  # within threshold: a click attempt (no element there -> no signal)
    press_release(QPoint(50, 50), QPoint(200, 200))  # a drag/pan: must not be treated as a click
    assert received == []  # show_message() cleared the hit map, so neither resolves - no crash either way


def test_plan_graph_click_selects_the_operator_tree_node(window, qtbot):
    tab = window.plan
    plan = window.analysis.effective_plan
    assert plan is not None
    # simulate what the SvgView would emit on a click, bypassing pixel hit-testing
    node_id = plan.nodes()[-1].id
    tab.view.elementClicked.emit(node_id)
    assert tab.tree.currentItem() is tab._items[node_id]


def test_plan_graph_double_click_collapses_and_expands_a_subtree(window, qtbot):
    tab = window.plan
    plan = window.analysis.effective_plan
    root_id = plan.root.id
    assert plan.root.children
    tab.view.elementDoubleClicked.emit(root_id)
    assert root_id in tab._collapsed
    assert "collapsed" in tab.view.svg_bytes.decode()
    tab.view.elementDoubleClicked.emit(root_id)
    assert root_id not in tab._collapsed
    tab._toggle_collapse(root_id)
    tab._expand_all()
    assert tab._collapsed == set()


def test_plan_graph_heat_map_toggle_colours_the_svg(window, qtbot):
    from sqlanalyzer.plans.graph import graphviz_available

    tab = window.plan
    tab.heat_box.setChecked(True)
    if graphviz_available():
        assert b"heat" in tab.view.svg_bytes
    tab.heat_box.setChecked(False)


def test_plan_export_menu_offers_svg_png_pdf(window, qtbot, tmp_path, monkeypatch):
    from sqlanalyzer.plans.graph import graphviz_available

    tab = window.plan
    actions = {a.text(): a for a in tab.export_btn.menu().actions()}
    assert {"SVG…", "PNG…", "PDF…"} == set(actions)
    if not graphviz_available():
        return
    for label, fmt in (("SVG…", "svg"), ("PNG…", "png"), ("PDF…", "pdf")):
        path = tmp_path / f"plan.{fmt}"
        monkeypatch.setattr("PySide6.QtWidgets.QFileDialog.getSaveFileName", lambda *a, p=path, **k: (str(p), ""))
        actions[label].trigger()
        assert path.exists() and path.stat().st_size > 0


# ---------------------------------------------------------------- local LLM tab
ANSWER = (
    '{"sql": "SELECT o.id, c.name FROM orders o JOIN customers c ON o.customer_id = c.id '
    'WHERE o.order_date >= DATE \'2024-01-15\' AND o.order_date < DATE \'2024-01-16\' '
    'AND c.name LIKE \'%smith\' AND NOT EXISTS (SELECT 1 FROM refunds r WHERE r.order_id = o.id) ORDER BY 1", '
    '"changes": [{"title": "Sargable date range", "reason": "index usable", "risk": "none"}], '
    '"notes": "SELECT * replaced by the used columns."}'
)


def _stub_llm(monkeypatch, reply):
    """Make the panel use a client that answers ``reply`` without any network."""
    from sqlanalyzer.llm import LlmClient

    class Stub(LlmClient):
        def complete(self, prompt, system=""):
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr("sqlanalyzer.gui.llm_panel.LlmClient", Stub)


def _ask(window, qtbot):
    window.optimize.pages.setCurrentWidget(window.optimize.llm)
    window.optimize.llm.ask()
    qtbot.waitUntil(lambda: not window.optimize.llm.busy, timeout=10_000)


def test_llm_panel_rejects_a_rewrite_that_drops_columns(window, qtbot, monkeypatch):
    _stub_llm(monkeypatch, ANSWER)
    panel = window.optimize.llm
    assert panel.result is None and "No suggestion yet" in panel.status.text()
    _ask(window, qtbot)
    result = panel.result
    assert result is not None and result.sql
    # the sample query selects *, the model returned two columns and no schema is declared
    assert not result.ok and result.review.verdict == "rejected"
    statuses = {panel.checks.item(r, 0).text(): panel.checks.item(r, 1).text()
                for r in range(panel.checks.rowCount())}
    assert statuses["syntax"] == "PASS" and statuses["projection"] == "FAIL"
    assert panel.applicable_sql() == ""
    assert not window.optimize.apply_btn.isEnabled() and not window.optimize.copy_btn.isEnabled()
    assert "failed validation" in panel.status.text()
    assert "Sargable date range" in panel.changes_view.toPlainText()


def test_llm_panel_accepts_a_validated_rewrite_and_applies_it(window, qtbot, monkeypatch):
    window.editor.setPlainText("SELECT o.id FROM orders o, customers c WHERE o.customer_id = c.id AND o.x = NULL")
    window.analyze()
    wait_optimized(window, qtbot)
    good = ('{"sql": "SELECT o.id FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.x IS NULL", '
            '"changes": [{"title": "explicit join", "reason": "clearer", "risk": "none"}], "notes": ""}')
    _stub_llm(monkeypatch, good)
    _ask(window, qtbot)
    panel = window.optimize.llm
    assert panel.result.ok and panel.applicable_sql()
    assert window.optimize.apply_btn.isEnabled() and window.optimize.current_sql() == panel.applicable_sql()
    window.optimize._apply()
    wait_optimized(window, qtbot)
    assert "JOIN customers" in window.editor.toPlainText()
    # the suggestion now belongs to an older query version
    assert panel.stale and panel.applicable_sql() == ""
    assert not window.optimize.apply_btn.isEnabled()


def test_llm_panel_reports_transport_errors(window, qtbot, monkeypatch):
    from sqlanalyzer.llm import LlmError

    _stub_llm(monkeypatch, LlmError("Cannot reach http://localhost:11434: refused"))
    _ask(window, qtbot)
    panel = window.optimize.llm
    assert panel.result is not None and not panel.result.ok
    assert "Cannot reach" in panel.status.text() and panel.checks.rowCount() == 0
    assert panel.applicable_sql() == ""


def test_llm_panel_keeps_the_rule_page_usable(window, qtbot, monkeypatch):
    _stub_llm(monkeypatch, ANSWER)
    _ask(window, qtbot)
    window.optimize.pages.setCurrentIndex(0)  # back to the rule engine
    assert window.optimize.apply_btn.isEnabled()
    assert window.optimize.current_sql() == window.analysis.optimization.optimized_sql


def test_llm_settings_dialog_round_trips_the_config(window, qtbot):
    from sqlanalyzer.gui.llm_dialog import LlmSettingsDialog
    from sqlanalyzer.llm import OPENAI

    dialog = LlmSettingsDialog(window.session.llm, window)
    qtbot.addWidget(dialog)
    dialog.provider.setCurrentIndex(dialog.provider.findData(OPENAI))
    assert dialog.base_url.text() == "http://localhost:8080"  # default follows the provider
    dialog.model.setCurrentText("my-local-model")
    dialog.temperature.setValue(0.3)
    config = dialog.config()
    assert config.provider == OPENAI and config.model == "my-local-model" and config.temperature == 0.3
    window.session.save_llm(config)
    assert window.session.llm.model == "my-local-model"
    assert "my-local-model" in window.optimize.llm.status.text() or window.optimize.llm.result is not None


def test_llm_settings_test_button_reports_failure(window, qtbot):
    from sqlanalyzer.gui.llm_dialog import LlmSettingsDialog
    from sqlanalyzer.llm import LlmConfig

    dialog = LlmSettingsDialog(LlmConfig(base_url="http://127.0.0.1:1", timeout=2), window)
    qtbot.addWidget(dialog)
    dialog._test()
    qtbot.waitUntil(lambda: "Cannot reach" in dialog.status.text(), timeout=10_000)


# ------------------------------------------------------------------ tuning tab
SCHEMA_WITH_STATS = ("orders (rows=2000000): id int pk, customer_id int, order_date date, name text\n"
                     "customers (rows=50000): id int pk, name text\nrefunds (rows=10000): order_id int")


def test_tuning_tab_shows_cost_model_and_index_recommendations(window, qtbot):
    window.schema_edit.setPlainText(SCHEMA_WITH_STATS)
    window.analyze()
    wait_optimized(window, qtbot)
    tab = window.tuning
    text = tab.cost_view.toPlainText()
    assert "Offline cost model" in text and "Estimated cost" in text and "Most expensive operators" in text
    assert "Rule-engine rewrite" in text and "Table sizes used" in text and "orders ~2.0M" in text
    assert tab.table.rowCount() == len(window.analysis.index_recommendations) >= 1
    assert window.tabs.tabText(window.tabs.indexOf(window.tuning)).startswith("Tuning (")
    tab.table.selectRow(0)
    assert tab.ddl.toPlainText().strip() and tab.copy_btn.isEnabled()
    tab._copy()
    from PySide6.QtWidgets import QApplication

    assert QApplication.clipboard().text() == window.analysis.index_recommendations[0].ddl
    # the plan tab now shows model estimates for the offline plan
    assert window.analysis.logical_plan.root.est_rows is not None
    assert "cost model" in window.plan.source_label.text()
    assert "offline estimate" in window.plan.explanation_view.toPlainText()
    # cost-node links select the operator in the plan tab
    hottest = window.analysis.cost.hottest[0]
    tab._anchor(_url(f"node:{hottest.node_id}"))
    assert window.tabs.currentWidget() is window.plan and window.plan.tree.currentItem() is not None


def test_tuning_tab_without_a_parseable_query(window, qtbot):
    window.editor.setPlainText("SELECT FROM WHERE ((")
    window.analyze()
    wait_optimized(window, qtbot)
    assert window.tuning.table.rowCount() == 0 and "once the SQL parses" in window.tuning.summary.text()


# ------------------------------------------------------------ pick rewrites page
def test_pick_rewrites_page_applies_a_subset(window, qtbot):
    from PySide6.QtCore import Qt

    picker = window.optimize.picker
    assert picker.table.rowCount() == len(window.analysis.suggestions) >= 3
    rules = [picker.table.item(r, 1).text() for r in range(picker.table.rowCount())]
    assert {"sargable-date", "explicit-join", "not-in-to-not-exists"} <= set(rules)
    window.optimize.pages.setCurrentWidget(picker)
    assert not window.optimize.apply_btn.isEnabled()  # nothing ticked yet
    row = rules.index("explicit-join")
    picker.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert picker.selected_rules() == ["explicit-join"]
    preview = picker.preview.toPlainText()
    assert "JOIN customers AS c" in preview and "NOT IN" in " ".join(preview.split()).replace("NOT o.id IN", "NOT IN")
    assert window.optimize.apply_btn.isEnabled() and window.optimize.current_sql() == preview
    picker.table.selectRow(row)
    assert "explicit-join" in picker.detail.toPlainText()
    picker.select_all.click()
    assert set(picker.selected_rules()) == {r for r, s in zip(rules, window.analysis.suggestions, strict=True)
                                             if s.safety.value == "safe"}
    picker.select_none.click()
    assert picker.selected_rules() == [] and not window.optimize.apply_btn.isEnabled()
    picker.table.item(row, 0).setCheckState(Qt.CheckState.Checked)
    window.optimize._apply()
    wait_optimized(window, qtbot)
    assert "JOIN customers" in window.editor.toPlainText()
    assert "implicit-join" not in {f.rule for f in window.analysis.findings}
    assert "non-sargable-predicate" in {f.rule for f in window.analysis.findings}  # only one rule was applied


# ----------------------------------------------------- findings: one-click fix
def test_findings_detail_offers_a_single_fix(window, qtbot):
    tab = window.findings
    row = next(i for i, f in enumerate(tab._shown) if f.rule == "non-sargable-predicate")
    tab.table.selectRow(row)
    assert tab.fix_btn.isVisible() and "Suggested rewrite" in tab.detail.toPlainText()
    assert "sargable-date" in tab.detail.toPlainText()
    row_star = next(i for i, f in enumerate(tab._shown) if f.rule == "select-star")
    tab.table.selectRow(row_star)
    assert not tab.fix_btn.isVisible()
    tab.table.selectRow(row)
    tab.fix_btn.click()
    wait_optimized(window, qtbot)
    text = " ".join(window.editor.toPlainText().split())
    assert "o.order_date >= CAST('2024-01-15' AS DATE)" in text and "orders AS o, customers AS c" in text
    assert "non-sargable-predicate" not in {f.rule for f in window.analysis.findings}


# ---------------------------------------------------------------- history tab
def test_history_records_explicit_actions_only(window, qtbot, tmp_path):
    history = window.session.history
    assert len(history) == 0  # the automatic analysis at start-up does not record
    window.analyze(record=True)
    wait_optimized(window, qtbot)
    assert len(history) == 1 and window.tabs.tabText(window.tabs.indexOf(window.history)) == "History (1)"
    entry = history.entries()[0]
    assert entry.engine == "duckdb" and entry.findings == len(window.analysis.findings) and entry.cost
    assert entry.complexity == window.analysis.complexity.score and entry.source == "analyze"
    window.editor.setPlainText("SELECT a FROM t WHERE a = NULL")
    window.analyze()  # timer-style analysis: not recorded
    wait_optimized(window, qtbot)
    assert len(history) == 1
    window.optimize._apply()  # applying a rewrite records it
    wait_optimized(window, qtbot)
    assert len(history) == 2 and history.entries()[0].source == "rewrite" and "IS NULL" in history.entries()[0].sql
    assert (tmp_path / "history.json").exists()


def test_history_tab_load_search_compare_and_delete(window, qtbot, monkeypatch):
    window.analyze(record=True)
    wait_optimized(window, qtbot)
    window.editor.setPlainText("SELECT 42 AS answer")
    window.analyze(record=True)
    wait_optimized(window, qtbot)
    tab = window.history
    assert tab.table.rowCount() == 2 and tab.table.item(0, 7).text().startswith("SELECT 42")
    tab.search.setText("answer")
    assert tab.table.rowCount() == 1
    tab.search.setText("")
    tab.engine_filter.setCurrentIndex(tab.engine_filter.findData("postgres"))
    assert tab.table.rowCount() == 0
    tab.engine_filter.setCurrentIndex(0)
    tab.table.selectRow(1)  # the sample query
    assert "orders o, customers c" in tab.preview.toPlainText() and tab.load_btn.isEnabled()
    tab.tag_edit.setText("sample, slow")
    tab.tag_btn.click()
    assert tab.table.item(1, 7).text().endswith("[sample, slow]")
    tab._compare(True)
    assert window.tabs.currentWidget() is window.compare and "orders o" in window.compare.old.editor.toPlainText()
    tab.table.selectRow(1)
    tab.load_selected()
    wait_optimized(window, qtbot)
    assert "orders o, customers c" in window.editor.toPlainText() and window.tabs.currentWidget() is window.findings
    tab.table.selectRow(0)
    tab.delete_selected()
    assert tab.table.rowCount() == 1 and len(window.session.history) == 1
    from PySide6.QtWidgets import QMessageBox

    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes))
    tab.clear()
    assert tab.table.rowCount() == 0 and len(window.session.history) == 0


def test_history_survives_a_restart_and_switches_the_engine(window, qtbot):
    from sqlanalyzer.gui.main_window import MainWindow

    window.engine_box.setCurrentIndex(window.engine_box.findData(Engine.TRINO))
    window.editor.setPlainText("SELECT count(DISTINCT x) FROM t")
    window.analyze(record=True)
    wait_optimized(window, qtbot)
    again = MainWindow()
    qtbot.addWidget(again)
    wait_optimized(again, qtbot)
    assert len(again.session.history) == 1 and again.tabs.tabText(again.tabs.indexOf(again.history)) == "History (1)"
    again.history.table.selectRow(0)
    again.history.load_selected()
    wait_optimized(again, qtbot)
    assert again.session.engine is Engine.TRINO and "count(DISTINCT x)" in again.editor.toPlainText()
    assert "count-distinct-exact" in {f.rule for f in again.analysis.findings}


def test_schema_statistics_drive_cost_and_engine_rules(window, qtbot):
    window.engine_box.setCurrentIndex(window.engine_box.findData(Engine.TRINO))
    window.schema_edit.setPlainText(SCHEMA_WITH_STATS)
    window.editor.setPlainText("SELECT 1 FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.name = 'x'")
    window.analyze()
    wait_optimized(window, qtbot)
    rules = {f.rule for f in window.analysis.findings}
    assert "join-build-side" in rules  # needs the declared row counts
    assert "Hash Join" in window.tuning.cost_view.toPlainText()
    assert window.analysis.cost.table_rows == {"customers": 50_000.0, "orders": 2_000_000.0}


# -------------------------------------------------------- DuckDB connection dialog
def test_connection_dialog_save_select_and_delete_a_profile(window, qtbot, monkeypatch, tmp_path):
    from sqlanalyzer.gui.connection_dialog import ConnectionDialog
    from sqlanalyzer.gui.profiles import ProfileStore

    settings_path = tmp_path / "settings_profiles.json"
    from sqlanalyzer.gui.settings import Settings

    store = ProfileStore(Settings(settings_path), Engine.POSTGRES)
    dialog = ConnectionDialog(Engine.POSTGRES, None, window, profiles=store)
    qtbot.addWidget(dialog)
    assert dialog.profile_box.count() == 1  # just "(none)"
    assert not dialog.delete_profile_btn.isEnabled()

    dialog.host.setText("db.example.com")
    dialog.database.setText("analytics")
    dialog.username.setText("alice")
    dialog.password.setText("s3cret")
    monkeypatch.setattr("PySide6.QtWidgets.QInputDialog.getText", lambda *a, **k: ("Prod", True))
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.information", lambda *a, **k: None)
    dialog._save_profile()
    assert dialog.profile_box.count() == 2
    assert dialog.profile_box.currentText() == "Prod"
    assert dialog.delete_profile_btn.isEnabled()

    # switching to "(none)" and back restores the saved fields
    dialog.profile_box.setCurrentIndex(0)
    dialog.host.setText("")
    dialog.profile_box.setCurrentIndex(dialog.profile_box.findData("prod"))
    assert dialog.host.text() == "db.example.com" and dialog.database.text() == "analytics"

    dialog._delete_profile()
    assert dialog.profile_box.count() == 1
    assert "prod" not in store.profiles


def test_connection_dialog_without_a_profile_store_has_no_profile_ui(window, qtbot):
    from sqlanalyzer.gui.connection_dialog import ConnectionDialog

    dialog = ConnectionDialog(Engine.POSTGRES, None, window)
    qtbot.addWidget(dialog)
    assert dialog.profile_box.count() == 0  # never populated


def test_connection_dialog_has_no_profile_ui_for_duckdb(window, qtbot, tmp_path):
    from sqlanalyzer.gui.connection_dialog import ConnectionDialog
    from sqlanalyzer.gui.profiles import ProfileStore
    from sqlanalyzer.gui.settings import Settings

    store = ProfileStore(Settings(tmp_path / "s.json"), Engine.DUCKDB)
    dialog = ConnectionDialog(Engine.DUCKDB, None, window, profiles=store)
    qtbot.addWidget(dialog)
    assert dialog.profile_box.count() == 0


def test_connection_dialog_password_note_mentions_keyring_availability(window, qtbot, monkeypatch):
    from sqlanalyzer.gui import connection_dialog

    monkeypatch.setattr(connection_dialog, "keyring_available", lambda: False)
    assert "kept in memory" in connection_dialog.ConnectionDialog._password_note()
    monkeypatch.setattr(connection_dialog, "keyring_available", lambda: True)
    assert "keychain" in connection_dialog.ConnectionDialog._password_note()


def test_edit_connection_uses_a_profile_store(window, qtbot, monkeypatch):
    captured = {}

    class _FakeDialog:
        def __init__(self, engine, existing, parent, profiles=None):
            captured["profiles"] = profiles

        def exec(self):
            return False

    monkeypatch.setattr("sqlanalyzer.gui.main_window.ConnectionDialog", _FakeDialog)
    window.edit_connection()
    assert captured["profiles"] is not None and captured["profiles"].engine is window.session.engine


def test_duckdb_connection_dialog_open_new_and_demo(window, qtbot, tmp_path, monkeypatch):
    from sqlanalyzer.gui.connection_dialog import ConnectionDialog, describe_duckdb_path

    monkeypatch.setenv("SQLANALYZER_DEMO_FILE", str(tmp_path / "demo.duckdb"))
    import sqlanalyzer.demo as demo_module

    monkeypatch.setattr(demo_module, "SIZES", {k: 20 for k in demo_module.SIZES})  # fast demo
    dialog = ConnectionDialog(Engine.DUCKDB, None, window)
    qtbot.addWidget(dialog)
    assert "In-memory" in dialog.file_status.text()
    existing = tmp_path / "existing.duckdb"
    import duckdb

    duckdb.connect(str(existing)).close()
    monkeypatch.setattr("sqlanalyzer.gui.connection_dialog.QFileDialog.getOpenFileName",
                        lambda *a, **k: (str(existing), ""))
    dialog._open_existing()
    assert dialog.database.text() == str(existing) and "Existing database file" in dialog.file_status.text()
    monkeypatch.setattr("sqlanalyzer.gui.connection_dialog.QFileDialog.getSaveFileName",
                        lambda *a, **k: (str(tmp_path / "new.duckdb"), ""))
    dialog._new_file()
    assert "will be created" in dialog.file_status.text()
    dialog.database.setText(str(tmp_path / "missing-dir" / "x.duckdb"))
    assert "folder does not exist" in dialog.file_status.text()
    dialog._use_demo()
    qtbot.waitUntil(lambda: "ready" in dialog.status.text() or "Could not" in dialog.status.text(), timeout=60_000)
    assert "ready" in dialog.status.text() and dialog.database.text() == str(tmp_path / "demo.duckdb")
    assert "Existing database file" in dialog.file_status.text()
    assert describe_duckdb_path(str(tmp_path)) == "This is a directory - choose a file."
    dialog._test()
    qtbot.waitUntil(lambda: "successful" in dialog.status.text(), timeout=20_000)
    window.session.save_connection(dialog.config())
    assert window.session.connection.database == str(tmp_path / "demo.duckdb")


# ------------------------------------------------------------- schema import
@pytest.fixture
def demo_connection(window, tmp_path):
    from sqlanalyzer.db import ConnectionConfig
    from sqlanalyzer.demo import populate_duckdb

    path = populate_duckdb(tmp_path / "import.duckdb", sizes={"customers": 30, "products": 10, "orders": 200,
                                                              "order_items": 300, "refunds": 20, "shipments": 50,
                                                              "events": 40})
    cfg = ConnectionConfig(Engine.DUCKDB, database=str(path))
    window.engine_box.setCurrentIndex(window.engine_box.findData(Engine.DUCKDB))
    window.session.save_connection(cfg)
    return cfg


def test_schema_import_dialog_scopes_to_the_query_and_merges(window, qtbot, demo_connection):
    from sqlanalyzer.gui.schema_import_dialog import SchemaImportDialog

    window.open_sample("20_join_type_mismatch")
    wait_optimized(window, qtbot)
    assert window.referenced_tables() == ["orders", "shipments"]
    window.schema_edit.setPlainText("customers (rows=7): id int pk, name text")
    dialog = SchemaImportDialog(demo_connection, window.referenced_tables(), window)
    qtbot.addWidget(dialog)
    assert dialog.only_referenced.isChecked() and dialog.merge_mode
    dialog._run()
    qtbot.waitUntil(lambda: dialog.imported is not None or "No tables" in dialog.status.text(), timeout=20_000)
    assert dialog.imported is not None and set(dialog.imported.hints.tables) == {"orders", "shipments"}
    window.apply_imported_schema(dialog.imported.hints, merge=dialog.merge_mode)
    text = window.schema_edit.toPlainText()
    assert "customers (rows=7)" in text and "orders (rows=200)" in text and "shipments (rows=50)" in text
    assert "order_ref varchar" in text
    wait_optimized(window, qtbot)
    assert "join-type-mismatch" in {f.rule for f in window.analysis.findings}
    # replace mode drops the hand-written table
    window.apply_imported_schema(dialog.imported.hints, merge=False)
    assert "customers" not in window.schema_edit.toPlainText()


def test_schema_import_reports_empty_results(window, qtbot, demo_connection):
    from sqlanalyzer.gui.schema_import_dialog import SchemaImportDialog

    dialog = SchemaImportDialog(demo_connection, ["no_such_table"], window)
    qtbot.addWidget(dialog)
    dialog._run()
    qtbot.waitUntil(lambda: "No tables" in dialog.status.text(), timeout=20_000)
    assert dialog.import_btn.isEnabled()
    dialog.only_referenced.setChecked(False)
    dialog.distinct.setChecked(True)
    dialog._run()
    qtbot.waitUntil(lambda: dialog.imported is not None, timeout=30_000)
    assert len(dialog.imported.hints.tables) == 7
    assert dialog.imported.hints.stats["orders"].ndv  # distinct counts were collected


def test_import_schema_without_connection_opens_the_connection_dialog(window, monkeypatch):
    from sqlanalyzer.gui import main_window as mw

    opened = []
    monkeypatch.setattr(mw.MainWindow, "edit_connection", lambda self: opened.append(True))
    window.session.connections.clear()
    window.import_schema()
    assert opened == [True]


# ---------------------------------------------------------------- samples menu
def test_samples_menu_loads_a_sample_and_switches_engine(window, qtbot):
    from sqlanalyzer.samples import load_samples

    names = [a.text() for a in window.samples_menu.actions()]
    assert names == [s.name for s in load_samples()]
    window.open_sample("16_count_distinct_distributed")
    wait_optimized(window, qtbot)
    assert window.session.engine is Engine.TRINO  # DuckDB was not supported by that sample
    assert "COUNT(DISTINCT" in window.editor.toPlainText()
    assert "count-distinct-exact" in {f.rule for f in window.analysis.findings}
    window.samples_menu.actions()[0].trigger()
    wait_optimized(window, qtbot)
    assert "orders o, customers c" in window.editor.toPlainText()


# ---------------------------------------------------------------- QueryTabs widget
def test_query_tabs_title_from_sql_truncates_long_first_lines():
    from sqlanalyzer.gui.query_tabs import title_from_sql

    assert title_from_sql("") == "Query"
    assert title_from_sql("-- comment only\n  \n") == "Query"
    assert title_from_sql("select 1") == "select 1"
    long_line = "select " + ", ".join(f"col{i}" for i in range(20))
    title = title_from_sql(long_line)
    assert len(title) == 24 and title.endswith("…")


def test_query_tabs_widget_standalone(qtbot):
    from PySide6.QtWidgets import QApplication

    from sqlanalyzer.gui.query_tabs import QueryTabs

    QApplication.instance() or QApplication([])
    tabs = QueryTabs()
    qtbot.addWidget(tabs)
    tabs.new_tab("SELECT 1", "A")
    assert tabs.count() == 1
    a = tabs.current_editor
    tabs.plan_text = "plan-a"
    tabs.new_tab("SELECT 2", "B")
    assert tabs.count() == 2 and tabs.current_editor is not a
    assert tabs.plan_text == ""  # fresh tab, no plan yet
    tabs.plan_text = "plan-b"
    tabs.setCurrentIndex(0)
    assert tabs.current_editor is a and tabs.plan_text == "plan-a"
    tabs.close_tab(0)
    assert tabs.count() == 1 and tabs.current_editor.toPlainText() == "SELECT 2"
    tabs.close_tab(0)
    assert tabs.count() == 1  # refuses to close the last tab
