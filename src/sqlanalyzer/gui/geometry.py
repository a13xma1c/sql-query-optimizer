"""Keep windows and dialogs inside the display.

Qt sizes a top-level window from its layout's size hints; a single unwrapped label
can push the minimum width past the screen. These helpers give windows an initial
size relative to the *available* screen area, clamp any saved geometry to the
screens that are actually present, and persist the geometry between runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtWidgets import QWidget

GEOMETRY_KEY = "window/geometry"
MAXIMIZED_KEY = "window/maximized"
DEFAULT_SIZE = (1360, 860)


def available_rect(widget: QWidget | None = None) -> QRect:
    """Usable area of the screen the widget is (or will be) on; falls back to the primary screen."""
    screen: QScreen | None = None
    if widget is not None:
        handle = widget.windowHandle()
        screen = handle.screen() if handle is not None else widget.screen()
    screen = screen or QGuiApplication.primaryScreen()
    return screen.availableGeometry() if screen is not None else QRect(0, 0, 1280, 800)


def fit_size(preferred: QSize, area: QRect, fraction: float = 0.9) -> QSize:
    """``preferred`` shrunk so it never exceeds ``fraction`` of ``area``."""
    return QSize(min(preferred.width(), int(area.width() * fraction)),
                 min(preferred.height(), int(area.height() * fraction)))


def clamp_rect(rect: QRect, screens: Sequence[QRect]) -> QRect | None:
    """Move/shrink ``rect`` into the screen it mostly overlaps; ``None`` if it is on no screen at all."""
    best = max(screens, key=lambda s: _overlap(rect, s), default=None)
    if best is None or _overlap(rect, best) <= 0:
        return None
    width = min(rect.width(), best.width())
    height = min(rect.height(), best.height())
    x = min(max(rect.x(), best.x()), best.right() - width + 1)
    y = min(max(rect.y(), best.y()), best.bottom() - height + 1)
    return QRect(x, y, width, height)


def _overlap(a: QRect, b: QRect) -> int:
    inter = a.intersected(b)
    return inter.width() * inter.height() if inter.isValid() else 0


def place_window(window: QWidget, settings=None, default: tuple[int, int] = DEFAULT_SIZE) -> None:
    """Restore the saved geometry (clamped to the current screens) or size the window to fit the display."""
    screens = [s.availableGeometry() for s in QGuiApplication.screens()] or [available_rect()]
    saved = settings.value(GEOMETRY_KEY) if settings is not None else None
    if isinstance(saved, list) and len(saved) == 4 and all(isinstance(v, int) for v in saved):
        rect = clamp_rect(QRect(*saved), screens)
        if rect is not None and rect.width() >= 400 and rect.height() >= 300:
            window.setGeometry(rect)
            if settings.value(MAXIMIZED_KEY, False):
                window.showMaximized()
            return
    area = screens[0]
    size = fit_size(QSize(*default), area)
    window.resize(size)
    window.move(area.x() + (area.width() - size.width()) // 2, area.y() + (area.height() - size.height()) // 2)


def save_window(window: QWidget, settings) -> None:
    if settings is None:
        return
    maximized = window.isMaximized()
    rect = window.normalGeometry() if maximized else window.geometry()
    settings.set_value(GEOMETRY_KEY, [rect.x(), rect.y(), rect.width(), rect.height()])
    settings.set_value(MAXIMIZED_KEY, maximized)


def fit_dialog(dialog: QWidget, width: int, height: int) -> None:
    """``dialog.resize`` that respects the screen of its parent."""
    area = available_rect(dialog.parentWidget() or dialog)
    dialog.resize(fit_size(QSize(width, height), area))
