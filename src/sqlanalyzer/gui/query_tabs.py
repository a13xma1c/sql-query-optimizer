"""Multiple independent SQL query tabs.

Each tab owns its own :class:`~sqlanalyzer.gui.editor.CodeEditor` plus the bits of state that
travel with *that* query rather than with the application as a whole: a pasted/fetched EXPLAIN
plan (``plan_text``) and the file it was loaded from/saved to (``file_path``). The rest of
``MainWindow`` keeps working against a single ``self.editor``/``self.plan_text`` - they become
properties that resolve to whichever tab is currently active, so nothing else has to know tabs
exist.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QInputDialog, QTabWidget, QToolButton

from ..optimizer.schema import SchemaHints
from .editor import CodeEditor
from .theme import Theme

DEFAULT_TITLE = "Query"
_MAX_TITLE = 24


@dataclass
class _TabState:
    plan_text: str = ""
    file_path: str | None = None


def title_from_sql(sql: str, fallback: str = DEFAULT_TITLE) -> str:
    """A short tab title guessed from the first non-comment, non-blank line of ``sql``."""
    for line in sql.splitlines():
        line = line.strip()
        if not line or line.startswith("--"):
            continue
        line = re.sub(r"\s+", " ", line)
        return line if len(line) <= _MAX_TITLE else line[: _MAX_TITLE - 1] + "…"
    return fallback


class QueryTabs(QTabWidget):
    """A closable, reorderable tab bar of :class:`CodeEditor` instances."""

    textChanged = Signal()  # the *active* editor's text changed
    tabSwitched = Signal(int)  # the current tab index changed

    def __init__(self, schema: Callable[[], SchemaHints | None] | None = None) -> None:
        super().__init__()
        self._schema = schema
        self._theme = Theme.SYSTEM
        self.setTabsClosable(True)
        self.setMovable(True)
        self.setDocumentMode(True)
        self.tabCloseRequested.connect(self.close_tab)
        self.currentChanged.connect(self.tabSwitched)
        self.tabBarDoubleClicked.connect(self._rename)
        self._state: dict[int, _TabState] = {}
        new_button = QToolButton()
        new_button.setText("+")
        new_button.setToolTip("New query tab (Ctrl+T)")
        new_button.clicked.connect(lambda: self.new_tab())
        self.setCornerWidget(new_button)

    # ---------------------------------------------------------------------- tabs
    def new_tab(self, text: str = "", title: str | None = None, file_path: str | None = None) -> CodeEditor:
        editor = CodeEditor("Type or paste a SQL query…")
        if text:
            editor.setPlainText(text)
        editor.set_theme(self._theme)
        editor.textChanged.connect(lambda ed=editor: self._editor_changed(ed))
        if self._schema is not None:
            from .completer import attach_completer

            attach_completer(editor, self._schema)
        index = self.addTab(editor, title or title_from_sql(text))
        # populated before setCurrentIndex() so a currentChanged() handler sees the final state
        self._state[id(editor)] = _TabState(file_path=file_path)
        self.setCurrentIndex(index)
        return editor

    def close_tab(self, index: int) -> None:
        if self.count() <= 1:
            return  # always keep at least one tab open
        editor = self.widget(index)
        self._state.pop(id(editor), None)
        self.removeTab(index)
        if editor is not None:
            editor.deleteLater()

    def _rename(self, index: int) -> None:
        if index < 0:
            return
        name, ok = QInputDialog.getText(self, "Rename tab", "Title:", text=self.tabText(index))
        if ok and name.strip():
            self.setTabText(index, name.strip())

    def _editor_changed(self, editor: CodeEditor) -> None:
        if editor is self.currentWidget():
            self.textChanged.emit()

    # --------------------------------------------------------------------- state
    @property
    def current_editor(self) -> CodeEditor:
        widget = self.currentWidget()
        if not isinstance(widget, CodeEditor):  # pragma: no cover - defensive; a tab always exists in practice
            widget = self.new_tab()
        return widget

    def editors(self) -> list[CodeEditor]:
        return [w for i in range(self.count()) if isinstance(w := self.widget(i), CodeEditor)]

    def _current_state(self) -> _TabState:
        return self._state.setdefault(id(self.current_editor), _TabState())

    @property
    def plan_text(self) -> str:
        return self._current_state().plan_text

    @plan_text.setter
    def plan_text(self, value: str) -> None:
        self._current_state().plan_text = value

    @property
    def file_path(self) -> str | None:
        return self._current_state().file_path

    @file_path.setter
    def file_path(self, value: str | None) -> None:
        self._current_state().file_path = value

    def retitle_current(self, title: str) -> None:
        self.setTabText(self.currentIndex(), title)

    def set_theme(self, theme: Theme) -> None:
        """Remember ``theme`` for tabs opened later and re-theme every tab open right now."""
        self._theme = theme
        for editor in self.editors():
            editor.set_theme(theme)
