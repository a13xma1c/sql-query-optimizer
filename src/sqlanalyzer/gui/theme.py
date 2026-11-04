"""Light/dark theme.

Qt's ``Fusion`` style plus a swapped ``QPalette`` themes every standard widget (buttons, tables,
menus, scroll bars, ...) consistently without per-widget stylesheets. The one place that needs
its own colours is the SQL syntax highlighter, since it paints explicit foreground colours for
keywords/strings/etc. rather than relying on the palette - :class:`HighlightPalette` holds a
light- and a dark-safe set of those, and :func:`apply_theme` is the single entry point both the
application chrome and the highlighter's colours are derived from.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory


class Theme(StrEnum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class HighlightPalette:
    """Foreground colours the :class:`~sqlanalyzer.gui.editor.SqlHighlighter` paints tokens with."""

    number: str
    call: str
    keyword: str
    string: str
    comment: str


LIGHT = HighlightPalette(number="#b45f06", call="#7c5cd8", keyword="#1f6feb", string="#0a7d36", comment="#6e7781")
DARK = HighlightPalette(number="#f0b35c", call="#c9a6ff", keyword="#6cb6ff", string="#8fd19e", comment="#9aa0a6")


def resolve(theme: Theme) -> Theme:
    """``theme`` with :attr:`Theme.SYSTEM` replaced by a best guess from the current QPalette."""
    if theme is not Theme.SYSTEM:
        return theme
    app = QApplication.instance()
    if not isinstance(app, QApplication):  # None, or a QCoreApplication without a GUI
        return Theme.LIGHT
    return Theme.DARK if app.palette().color(QPalette.ColorRole.Window).lightness() < 128 else Theme.LIGHT


def highlight_palette(theme: Theme) -> HighlightPalette:
    return DARK if resolve(theme) is Theme.DARK else LIGHT


def dark_qpalette() -> QPalette:
    """A QPalette close to common dark IDE themes; also used to detect a dark desktop theme."""
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: "#2b2d30", QPalette.ColorRole.WindowText: "#e6e6e6",
        QPalette.ColorRole.Base: "#1e1f22", QPalette.ColorRole.AlternateBase: "#2b2d30",
        QPalette.ColorRole.ToolTipBase: "#e6e6e6", QPalette.ColorRole.ToolTipText: "#1e1f22",
        QPalette.ColorRole.Text: "#e6e6e6", QPalette.ColorRole.Button: "#3c3f41",
        QPalette.ColorRole.ButtonText: "#e6e6e6", QPalette.ColorRole.BrightText: "#ff6b68",
        QPalette.ColorRole.Link: "#6cb6ff", QPalette.ColorRole.Highlight: "#3b6ea5",
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.PlaceholderText: "#9aa0a6",
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    disabled = QColor("#7a7a7a")
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, disabled)
    return p


def apply_theme(app: QApplication, theme: Theme) -> None:
    """Switch the whole application's style/palette. Safe to call repeatedly (e.g. on every toggle)."""
    fusion = QStyleFactory.create("Fusion")
    if fusion is not None:
        app.setStyle(fusion)
    if resolve(theme) is Theme.DARK:
        app.setPalette(dark_qpalette())
    else:
        app.setPalette(app.style().standardPalette())
