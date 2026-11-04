"""Unit tests for the light/dark theme module."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtGui import QPalette  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from sqlanalyzer.gui.theme import DARK, LIGHT, Theme, apply_theme, dark_qpalette, highlight_palette, resolve  # noqa: E402


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_resolve_passes_through_explicit_themes():
    assert resolve(Theme.LIGHT) is Theme.LIGHT
    assert resolve(Theme.DARK) is Theme.DARK


def test_resolve_system_uses_the_current_palette_lightness(app):
    app.setPalette(dark_qpalette())
    assert resolve(Theme.SYSTEM) is Theme.DARK
    app.setPalette(app.style().standardPalette())


def test_highlight_palette_differs_between_themes():
    assert highlight_palette(Theme.LIGHT) is LIGHT
    assert highlight_palette(Theme.DARK) is DARK
    assert LIGHT != DARK


def test_dark_qpalette_is_actually_dark():
    p = dark_qpalette()
    assert p.color(QPalette.ColorRole.Window).lightness() < 128
    assert p.color(QPalette.ColorRole.WindowText).lightness() > 128


def test_apply_theme_sets_a_dark_palette(app):
    apply_theme(app, Theme.DARK)
    assert app.palette().color(QPalette.ColorRole.Window).lightness() < 128
    apply_theme(app, Theme.LIGHT)
    assert app.palette().color(QPalette.ColorRole.Window).lightness() >= 128


def test_apply_theme_is_idempotent_and_does_not_raise(app):
    for _ in range(3):
        apply_theme(app, Theme.DARK)
        apply_theme(app, Theme.LIGHT)
        apply_theme(app, Theme.SYSTEM)
