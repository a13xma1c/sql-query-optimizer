"""SQL code editor: line numbers, syntax highlighting, error underlining, diagnostics tooltips."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QPoint, QRect, QRegularExpression, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from PySide6.QtWidgets import QAbstractItemView, QCompleter, QPlainTextEdit, QTextEdit, QToolTip, QWidget

from ..models import Finding, Severity
from .theme import LIGHT, HighlightPalette, Theme

# Severity is shown through both colour *and* underline shape/glyph so the editor remains legible
# without colour (e.g. for colour-blind users or a grayscale screenshot).
_SEVERITY_STYLE = {
    Severity.ERROR: (QTextCharFormat.UnderlineStyle.WaveUnderline, "#d73027", "\u26d4"),
    Severity.WARNING: (QTextCharFormat.UnderlineStyle.DashUnderline, "#e0a100", "\u26a0"),
    Severity.INFO: (QTextCharFormat.UnderlineStyle.DotLine, "#3b82d4", "\u2139"),
}

CandidatesFn = Callable[[str, str], tuple[str, list[str]]]

KEYWORDS = (
    "select from where group by order having limit offset join inner left right full outer cross on using as and or "
    "not in is null like ilike between exists case when then else end union all intersect except distinct with "
    "insert into values update set delete create table view over partition rows range asc desc true false cast "
    "lateral unnest qualify fetch first next only filter within pivot unpivot"
).split()
_KEYWORDS = KEYWORDS  # backward-compatible alias


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document, palette: HighlightPalette = LIGHT) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self.set_palette(palette)

    def set_palette(self, palette: HighlightPalette) -> None:
        """Swap token colours (e.g. on a light/dark theme change) and repaint."""

        def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
            f = QTextCharFormat()
            f.setForeground(QColor(color))
            if bold:
                f.setFontWeight(QFont.Weight.Bold)
            f.setFontItalic(italic)
            return f

        kw = r"\b(?:" + "|".join(_KEYWORDS) + r")\b"
        opts = QRegularExpression.PatternOption.CaseInsensitiveOption
        self._rules = [
            (QRegularExpression(r"\b\d+(?:\.\d+)?\b"), fmt(palette.number)),
            (QRegularExpression(r"\b[A-Za-z_][\w$]*(?=\s*\()"), fmt(palette.call)),
            (QRegularExpression(kw, opts), fmt(palette.keyword, bold=True)),
            (QRegularExpression(r"'(?:[^']|'')*'"), fmt(palette.string)),
            (QRegularExpression(r'"[^"]*"'), fmt(palette.string)),
            (QRegularExpression(r"--[^\n]*"), fmt(palette.comment, italic=True)),
        ]
        self._block = fmt(palette.comment, italic=True)
        self._start = QRegularExpression(r"/\*")
        self._end = QRegularExpression(r"\*/")
        self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        for rx, f in self._rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)
        # multi-line /* ... */ comments
        self.setCurrentBlockState(0)
        start = 0 if self.previousBlockState() == 1 else self._start.match(text).capturedStart()
        while start >= 0:
            end = self._end.match(text, start)
            if end.hasMatch():
                length = end.capturedEnd() - start
            else:
                self.setCurrentBlockState(1)
                length = len(text) - start
            self.setFormat(start, length, self._block)
            start = self._start.match(text, start + length).capturedStart()


class _LineNumbers(QWidget):
    def __init__(self, editor: CodeEditor) -> None:
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.line_number_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_line_numbers(event)


@dataclass
class _Diagnostic:
    start: int  # absolute document position
    end: int
    finding: Finding


class CodeEditor(QPlainTextEdit):
    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._numbers = _LineNumbers(self)
        self._marks: list[QTextEdit.ExtraSelection] = []
        self._diagnostics: list[_Diagnostic] = []
        self._highlighter = SqlHighlighter(self.document())
        self._completer: QCompleter | None = None
        # (sql, text_before_cursor) -> (prefix, options); see .completer
        self._candidates_fn: CandidatesFn | None = None
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._scroll_numbers)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self.setMouseTracking(True)
        self._update_margin()

    # -- autocomplete -----------------------------------------------------------------
    def set_completer(self, completer: QCompleter, candidates_fn: CandidatesFn) -> None:
        """Attach a popup completer; ``candidates_fn(sql, text_before_cursor) -> (prefix, options)``
        decides what to offer (see :mod:`sqlanalyzer.gui.completer`), keeping this class itself free
        of any schema-specific logic."""
        if self._completer is not None:
            self._completer.activated.disconnect(self._insert_completion)
        self._completer = completer
        self._candidates_fn = candidates_fn
        completer.setWidget(self)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.activated.connect(self._insert_completion)

    def _insert_completion(self, text: str) -> None:
        if self._completer is None:
            return
        cursor = self.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Left, QTextCursor.MoveMode.KeepAnchor,
                            len(self._completer.completionPrefix()))
        cursor.insertText(text)
        self.setTextCursor(cursor)

    def _text_before_cursor(self) -> str:
        cursor = self.textCursor()
        return cursor.block().text()[: cursor.positionInBlock()]

    def _popup(self) -> QAbstractItemView | None:
        """The completer's list view (``None`` without a completer; Qt always creates one otherwise)."""
        return self._completer.popup() if self._completer is not None else None

    def _update_completer_popup(self) -> None:
        popup = self._popup()
        if self._completer is None or self._candidates_fn is None or popup is None:
            return
        prefix, options = self._candidates_fn(self.toPlainText(), self._text_before_cursor())
        if not options:
            popup.hide()
            return
        from PySide6.QtCore import QStringListModel

        self._completer.setModel(QStringListModel(options, self._completer))
        self._completer.setCompletionPrefix(prefix)
        if self._completer.completionCount() == 0:
            popup.hide()
            return
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(popup.sizeHintForColumn(0) + popup.verticalScrollBar().sizeHint().width())
        self._completer.complete(rect)

    # -- line numbers -----------------------------------------------------------------
    def line_number_width(self) -> int:
        digits = max(2, len(str(self.blockCount())))
        return 12 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margin(self) -> None:
        self.setViewportMargins(self.line_number_width(), 0, 0, 0)

    def _scroll_numbers(self, rect, dy) -> None:
        if dy:
            self._numbers.scroll(0, dy)
        else:
            self._numbers.update(0, rect.y(), self._numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_margin()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._numbers.setGeometry(QRect(cr.left(), cr.top(), self.line_number_width(), cr.height()))

    def paint_line_numbers(self, event) -> None:
        p = QPainter(self._numbers)
        p.fillRect(event.rect(), self.palette().alternateBase())
        p.setPen(QColor("#8c959f"))
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        while block.isValid() and top <= event.rect().bottom():
            bottom = top + round(self.blockBoundingRect(block).height())
            if block.isVisible() and bottom >= event.rect().top():
                p.drawText(0, top, self._numbers.width() - 6, self.fontMetrics().height(),
                           Qt.AlignmentFlag.AlignRight, str(block.blockNumber() + 1))
            block = block.next()
            top = bottom
        p.end()

    # -- diagnostics ------------------------------------------------------------------
    def mark_findings(self, findings: list[Finding]) -> None:
        """Underline the lines of findings that carry a position.

        Severity is encoded redundantly as colour *and* underline style (wave/dash/dot), so the
        distinction still reads without colour. Hovering an underlined span shows the finding's
        message as a tooltip (see :meth:`event`).
        """
        self._marks = []
        self._diagnostics = []
        for f in sorted(findings, key=lambda f: -f.severity.rank):  # errors painted last (on top)
            if not f.line:
                continue
            block = self.document().findBlockByNumber(f.line - 1)
            if not block.isValid():
                continue
            style, color, _glyph = _SEVERITY_STYLE[f.severity]
            sel = QTextEdit.ExtraSelection()
            sel.cursor = self.textCursor()
            start = block.position() + max((f.col or 1) - 1, 0)
            sel.cursor.setPosition(start)
            sel.cursor.movePosition(sel.cursor.MoveOperation.EndOfWord, sel.cursor.MoveMode.KeepAnchor)
            if not sel.cursor.hasSelection():
                sel.cursor.setPosition(block.position())
                sel.cursor.movePosition(sel.cursor.MoveOperation.EndOfBlock, sel.cursor.MoveMode.KeepAnchor)
            sel.format.setUnderlineStyle(style)
            sel.format.setUnderlineColor(QColor(color))
            self._marks.append(sel)
            self._diagnostics.append(_Diagnostic(start, sel.cursor.position(), f))
        self._refresh_selections()

    def clear_marks(self) -> None:
        self._marks = []
        self._diagnostics = []
        self._refresh_selections()

    def diagnostic_at(self, pos: QPoint) -> Finding | None:
        """The finding whose underline contains the widget position ``pos``, if any."""
        cursor = self.cursorForPosition(pos)
        offset = cursor.position()
        for diag in self._diagnostics:
            if diag.start <= offset <= diag.end:
                return diag.finding
        return None

    def event(self, event) -> bool:  # noqa: N802 - Qt override
        from PySide6.QtCore import QEvent

        if event.type() == QEvent.Type.ToolTip:
            finding = self.diagnostic_at(event.pos())
            if finding is not None:
                _style, _color, glyph = _SEVERITY_STYLE[finding.severity]
                text = f"{glyph} [{finding.rule}] {finding.message}"
                if finding.suggestion:
                    text += f"\n\n{finding.suggestion}"
                QToolTip.showText(event.globalPos(), text, self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def _refresh_selections(self) -> None:
        current = QTextEdit.ExtraSelection()
        current.format.setBackground(QColor(59, 130, 212, 28))
        current.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        current.cursor = self.textCursor()
        current.cursor.clearSelection()
        self.setExtraSelections([current, *self._marks])

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt override
        popup = self._popup()
        if popup is not None and popup.isVisible() and event.key() in (
            Qt.Key.Key_Enter, Qt.Key.Key_Return, Qt.Key.Key_Escape, Qt.Key.Key_Tab, Qt.Key.Key_Backtab,
        ):
            event.ignore()  # QCompleter itself (installed as an event filter) handles these
            return
        is_shortcut = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.key() == Qt.Key.Key_Space
        if popup is None or not is_shortcut:
            super().keyPressEvent(event)
        if popup is None:
            return
        ctrl_or_shift = bool(event.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                                  | Qt.KeyboardModifier.ShiftModifier))
        if ctrl_or_shift and not event.text():
            return
        has_modifier = event.modifiers() != Qt.KeyboardModifier.NoModifier and not ctrl_or_shift
        if not is_shortcut and (has_modifier or not event.text()):
            popup.hide()
            return
        self._update_completer_popup()

    def set_theme(self, theme: Theme) -> None:
        """Re-derive the syntax highlighter's colours for a light/dark/system theme."""
        from .theme import highlight_palette

        self._highlighter.set_palette(highlight_palette(theme))

    def goto(self, line: int, col: int | None = None) -> None:
        block = self.document().findBlockByNumber(max(line - 1, 0))
        if not block.isValid():
            return
        cursor = self.textCursor()
        cursor.setPosition(block.position() + max((col or 1) - 1, 0))
        self.setTextCursor(cursor)
        self.setFocus()
        self.centerCursor()


def strip_trailing_semicolon(sql: str) -> str:
    return re.sub(r";\s*$", "", sql.strip())
