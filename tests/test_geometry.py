"""Windows must fit the display; DuckDB files must be written in a portable storage format."""

import json

import duckdb
import pytest
from PySide6.QtCore import QRect, QSize

from sqlanalyzer.db import DUCKDB_STORAGE_VERSION, ConnectionConfig, duckdb_storage_version
from sqlanalyzer.demo import populate_duckdb
from sqlanalyzer.dialects import Engine
from sqlanalyzer.gui.geometry import GEOMETRY_KEY, MAXIMIZED_KEY, clamp_rect, fit_size
from sqlanalyzer.gui.settings import Settings

pytest.importorskip("pytestqt")

SMALL = {"customers": 10, "products": 5, "orders": 50, "order_items": 60, "refunds": 5, "shipments": 20, "events": 30}


# ---------------------------------------------------------------- geometry maths
def test_fit_size_never_exceeds_the_screen_fraction():
    area = QRect(0, 0, 1000, 700)
    assert fit_size(QSize(1360, 860), area) == QSize(900, 630)
    assert fit_size(QSize(400, 300), area) == QSize(400, 300)


def test_clamp_rect_moves_and_shrinks_into_a_screen():
    screens = [QRect(0, 0, 1280, 800), QRect(1280, 0, 1920, 1080)]
    assert clamp_rect(QRect(100, 100, 800, 600), screens) == QRect(100, 100, 800, 600)
    assert clamp_rect(QRect(900, 500, 800, 600), screens) == QRect(1280, 480, 800, 600)  # mostly on screen 2
    assert clamp_rect(QRect(700, 300, 800, 600), screens) == QRect(480, 200, 800, 600)  # mostly on screen 1
    assert clamp_rect(QRect(-3000, -3000, 800, 600), screens) is None  # on no screen at all
    assert clamp_rect(QRect(1300, 0, 5000, 5000), screens) == QRect(1280, 0, 1920, 1080)  # bigger than the screen


# -------------------------------------------------------------- the main window
@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SQLANALYZER_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("SQLANALYZER_HISTORY_FILE", str(tmp_path / "history.json"))
    return tmp_path / "settings.json"


def test_main_window_fits_the_display_and_has_a_sane_minimum(qtbot, settings_env):
    from PySide6.QtGui import QGuiApplication

    from sqlanalyzer.gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not window.optimizing, timeout=20_000)
    area = QGuiApplication.primaryScreen().availableGeometry()
    assert window.width() <= area.width() and window.height() <= area.height()
    assert area.contains(window.frameGeometry().topLeft())
    assert window.minimumSizeHint().width() <= 600  # no unwrapped label dictates the width any more
    # descriptive labels wrap instead of widening the window
    from PySide6.QtWidgets import QLabel

    for label in window.findChildren(QLabel):
        assert len(label.text()) < 120 or label.wordWrap(), label.text()[:60]


def test_window_geometry_is_saved_and_restored_within_the_screen(qtbot, settings_env):
    from sqlanalyzer.gui.main_window import MainWindow

    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    qtbot.waitUntil(lambda: not window.optimizing, timeout=20_000)
    # a little above the window's own minimum, whatever the toolbar's exact content dictates today
    height = window.minimumSizeHint().height() + 20
    window.setGeometry(30, 40, 640, height)
    window.close()
    saved = json.loads(settings_env.read_text())
    assert saved[GEOMETRY_KEY] == [30, 40, 640, height] and saved[MAXIMIZED_KEY] is False
    again = MainWindow()
    qtbot.addWidget(again)
    again.show()
    qtbot.waitUntil(lambda: not again.optimizing, timeout=20_000)
    assert (again.width(), again.height()) == (640, height)
    # a geometry from a screen that is no longer attached falls back to a centred default
    settings = Settings(settings_env)
    settings.set_value(GEOMETRY_KEY, [-9000, -9000, 640, height])
    third = MainWindow()
    qtbot.addWidget(third)
    third.show()
    qtbot.waitUntil(lambda: not third.optimizing, timeout=20_000)
    from PySide6.QtGui import QGuiApplication

    area = QGuiApplication.primaryScreen().availableGeometry()
    assert area.contains(third.geometry().topLeft()) and third.width() <= area.width()


# -------------------------------------------------------------- DuckDB storage
def test_demo_files_use_the_portable_storage_format(tmp_path):
    path = populate_duckdb(tmp_path / "demo.duckdb", sizes=SMALL)
    assert duckdb_storage_version(path) == (64, "v1.0.0")
    assert duckdb.connect(str(path), read_only=True).execute("SELECT count(*) FROM orders").fetchone()[0] == 50
    newer = populate_duckdb(tmp_path / "newer.duckdb", sizes=SMALL, storage_version="v1.2.0")
    assert duckdb_storage_version(newer)[0] == 65
    assert duckdb_storage_version(tmp_path / "missing.duckdb") is None
    (tmp_path / "not.duckdb").write_bytes(b"nope")
    assert duckdb_storage_version(tmp_path / "not.duckdb") is None


def test_files_created_through_the_app_connection_are_portable(tmp_path):
    from sqlalchemy import create_engine, text

    cfg = ConnectionConfig(Engine.DUCKDB, database=str(tmp_path / "new.duckdb"))
    assert f"storage_compatibility_version={DUCKDB_STORAGE_VERSION}" in cfg.url().render_as_string()
    assert cfg.display() == f"duckdb:///{tmp_path / 'new.duckdb'}"  # the detail is hidden from the user
    engine = create_engine(cfg.url())
    with engine.connect() as conn:
        conn.execute(text("CREATE TABLE t AS SELECT 1 AS a"))
        conn.commit()
    engine.dispose()
    assert duckdb_storage_version(tmp_path / "new.duckdb") == (64, "v1.0.0")
    # a file written by DuckDB's default (latest) format still opens and keeps its version
    latest = tmp_path / "latest.duckdb"
    con = duckdb.connect(str(latest))
    con.execute("CREATE TABLE t AS SELECT 1 AS a")
    con.close()
    before = duckdb_storage_version(latest)
    engine = create_engine(ConnectionConfig(Engine.DUCKDB, database=str(latest)).url())
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM t")).scalar() == 1
    engine.dispose()
    assert duckdb_storage_version(latest) == before
    # the connection dialog tells the user which format a file has
    from sqlanalyzer.gui.connection_dialog import describe_duckdb_path

    assert "storage format 64 = DuckDB v1.0.0" in describe_duckdb_path(str(tmp_path / "new.duckdb"))


def test_cli_storage_version_flag(tmp_path, capsys):
    from sqlanalyzer.demo import main

    assert main(["duckdb", str(tmp_path / "cli.duckdb"), "--scale", "0.001", "--storage-version", "v1.2.0"]) == 0
    assert "storage format v1.2.0" in capsys.readouterr().out
    assert duckdb_storage_version(tmp_path / "cli.duckdb")[0] == 65
