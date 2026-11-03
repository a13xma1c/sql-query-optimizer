"""SQL code editor: line numbers, syntax highlighting, error underlining."""

from __future__ import annotations

import re

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QSyntaxHighlighter, QTextCharFormat, QTextFormat
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit, QWidget

from ..models import Finding, Severity

_KEYWORDS = (
    "select from where group by order having limit offset join inner left right full outer cross on using as and or "
    "not in is null like ilike between exists case when then else end union all intersect except distinct with "
    "insert into values update set delete create table view over partition rows range asc desc true false cast "
    "lateral unnest qualify fetch first next only filter within pivot unpivot"
).split()


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []

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
            (QRegularExpression(r"\b\d+(?:\.\d+)?\b"), fmt("#b45f06")),
            (QRegularExpression(r"\b[A-Za-z_][\w$]*(?=\s*\()"), fmt("#7c5cd8")),
            (QRegularExpression(kw, opts), fmt("#1f6feb", bold=True)),
            (QRegularExpression(r"'(?:[^']|'')*'"), fmt("#0a7d36")),
            (QRegularExpression(r'"[^"]*"'), fmt("#0a7d36")),
            (QRegularExpression(r"--[^\n]*"), fmt("#6e7781", italic=True)),
        ]
        self._block = fmt("#6e7781", italic=True)
        self._start = QRegularExpression(r"/\*")
        self._end = QRegularExpression(r"\*/")

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


class CodeEditor(QPlainTextEdit):
    def __init__(self, placeholder: str = "") -> None:
        super().__init__()
        self.setPlaceholderText(placeholder)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self._numbers = _LineNumbers(self)
        self._marks: list[QTextEdit.ExtraSelection] = []
        self._highlighter = SqlHighlighter(self.document())
        self.blockCountChanged.connect(self._update_margin)
        self.updateRequest.connect(self._scroll_numbers)
        self.cursorPositionChanged.connect(self._refresh_selections)
        self._update_margin()

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
        """Underline the lines of findings that carry a position."""
        self._marks = []
        colors = {Severity.ERROR: "#d73027", Severity.WARNING: "#e0a100", Severity.INFO: "#3b82d4"}
        for f in sorted(findings, key=lambda f: -f.severity.rank):  # errors painted last (on top)
            if not f.line:
                continue
            block = self.document().findBlockByNumber(f.line - 1)
            if not block.isValid():
                continue
            sel = QTextEdit.ExtraSelection()
            sel.cursor = self.textCursor()
            sel.cursor.setPosition(block.position() + max((f.col or 1) - 1, 0))
            sel.cursor.movePosition(sel.cursor.MoveOperation.EndOfWord, sel.cursor.MoveMode.KeepAnchor)
            if not sel.cursor.hasSelection():
                sel.cursor.setPosition(block.position())
                sel.cursor.movePosition(sel.cursor.MoveOperation.EndOfBlock, sel.cursor.MoveMode.KeepAnchor)
            sel.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            sel.format.setUnderlineColor(QColor(colors[f.severity]))
            self._marks.append(sel)
        self._refresh_selections()

    def clear_marks(self) -> None:
        self._marks = []
        self._refresh_selections()

    def _refresh_selections(self) -> None:
        current = QTextEdit.ExtraSelection()
        current.format.setBackground(QColor(59, 130, 212, 28))
        current.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        current.cursor = self.textCursor()
        current.cursor.clearSelection()
        self.setExtraSelections([current, *self._marks])

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
