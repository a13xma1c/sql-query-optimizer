"""Unit tests for the SQL code editor widget: underlining, tooltips, severity encoding."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QPoint  # noqa: E402
from PySide6.QtGui import QTextCharFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sqlanalyzer.gui.editor import CodeEditor, strip_trailing_semicolon  # noqa: E402
from sqlanalyzer.models import Category, Finding, Severity  # noqa: E402


@pytest.fixture
def editor(qtbot):
    QApplication.instance() or QApplication([])
    ed = CodeEditor()
    qtbot.addWidget(ed)
    ed.setPlainText("SELECT *\nFROM orders\nWHERE 1 = 1\n")
    return ed


def test_mark_findings_creates_one_selection_per_located_finding(editor):
    findings = [
        Finding("select-star", Severity.INFO, Category.LINT, "avoid select *", line=1, col=8),
        Finding("missing-where", Severity.ERROR, Category.LINT, "needs a filter", line=3, col=1),
    ]
    editor.mark_findings(findings)
    assert len(editor._marks) == 2
    assert len(editor.extraSelections()) == 3  # + the current-line highlight


def test_severity_is_encoded_by_style_as_well_as_colour(editor):
    findings = [
        Finding("r1", Severity.ERROR, Category.LINT, "e", line=1, col=1),
        Finding("r2", Severity.WARNING, Category.LINT, "w", line=2, col=1),
        Finding("r3", Severity.INFO, Category.LINT, "i", line=3, col=1),
    ]
    editor.mark_findings(findings)
    styles = {sel.format.underlineStyle() for sel in editor._marks}
    # three different severities must render with three different underline shapes
    assert styles == {
        QTextCharFormat.UnderlineStyle.WaveUnderline,
        QTextCharFormat.UnderlineStyle.DashUnderline,
        QTextCharFormat.UnderlineStyle.DotLine,
    }


def test_clear_marks_removes_diagnostics(editor):
    editor.mark_findings([Finding("select-star", Severity.INFO, Category.LINT, "msg", line=1, col=1)])
    assert editor._diagnostics
    editor.clear_marks()
    assert not editor._diagnostics and not editor._marks


def test_diagnostic_at_finds_the_finding_under_a_position(editor):
    finding = Finding("select-star", Severity.INFO, Category.LINT, "avoid select *", line=1, col=8)
    editor.mark_findings([finding])
    diag = editor._diagnostics[0]
    cursor = editor.textCursor()
    cursor.setPosition(diag.start)
    rect = editor.cursorRect(cursor)
    found = editor.diagnostic_at(QPoint(rect.left() + 1, rect.top() + 1))
    assert found is finding


def test_diagnostic_at_returns_none_far_from_any_mark(editor):
    editor.mark_findings([Finding("select-star", Severity.INFO, Category.LINT, "msg", line=1, col=1)])
    assert editor.diagnostic_at(QPoint(0, 10_000)) is None


def test_tooltip_event_shows_text_for_a_diagnostic(editor, monkeypatch):
    finding = Finding("select-star", Severity.INFO, Category.LINT, "avoid select *", "use explicit columns",
                      line=1, col=8)
    editor.mark_findings([finding])
    diag = editor._diagnostics[0]
    cursor = editor.textCursor()
    cursor.setPosition(diag.start)
    rect = editor.cursorRect(cursor)

    shown = {}

    def fake_show(pos, text, widget=None):
        shown["text"] = text

    monkeypatch.setattr("sqlanalyzer.gui.editor.QToolTip.showText", fake_show)
    # QHelpEvent (a ToolTip event with a position) is what Qt actually delivers on hover
    from PySide6.QtGui import QHelpEvent

    help_event = QHelpEvent(QEvent.Type.ToolTip, QPoint(rect.left() + 1, rect.top() + 1),
                            editor.mapToGlobal(QPoint(rect.left() + 1, rect.top() + 1)))
    editor.event(help_event)
    assert "select-star" in shown["text"] and "avoid select *" in shown["text"]
    assert "use explicit columns" in shown["text"]


def test_set_theme_changes_the_highlighter_colours(editor):
    from sqlanalyzer.gui.theme import DARK, LIGHT, Theme

    editor.set_theme(Theme.LIGHT)
    light_rules = dict(editor._highlighter._rules)
    editor.set_theme(Theme.DARK)
    dark_rules = dict(editor._highlighter._rules)
    assert len(light_rules) == len(dark_rules)
    # at least the keyword colour must differ between the two palettes
    assert LIGHT.keyword != DARK.keyword


def test_strip_trailing_semicolon():
    assert strip_trailing_semicolon("select 1;  \n") == "select 1"
    assert strip_trailing_semicolon("select 1") == "select 1"


# ------------------------------------------------------------------------- autocomplete
def test_set_completer_and_ctrl_space_shows_the_popup(editor):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QCompleter

    from sqlanalyzer.gui.completer import candidates
    from sqlanalyzer.optimizer.schema import parse_schema

    hints = parse_schema("orders: id int, customer_id int")
    editor.set_completer(QCompleter(editor), lambda sql, before: candidates(hints, sql, before))
    editor.setPlainText("SELECT * FROM ord")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    event = QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier, " ")
    editor.keyPressEvent(event)
    assert editor._completer.popup().isVisible()
    assert "orders" in [editor._completer.completionModel().index(i, 0).data()
                        for i in range(editor._completer.completionModel().rowCount())]


def test_completion_is_inserted_and_replaces_the_typed_prefix(editor):
    from PySide6.QtWidgets import QCompleter

    editor.set_completer(QCompleter(editor), lambda sql, before: ("ord", ["orders"]))
    editor.setPlainText("SELECT * FROM ord")
    cursor = editor.textCursor()
    cursor.movePosition(cursor.MoveOperation.End)
    editor.setTextCursor(cursor)
    editor._completer.setCompletionPrefix("ord")
    editor._insert_completion("orders")
    assert editor.toPlainText() == "SELECT * FROM orders"


def test_set_completer_can_be_replaced(editor):
    from PySide6.QtWidgets import QCompleter

    c1 = QCompleter(editor)
    editor.set_completer(c1, lambda sql, before: ("", []))
    c2 = QCompleter(editor)
    editor.set_completer(c2, lambda sql, before: ("", []))
    assert editor._completer is c2
