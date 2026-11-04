"""Interpreter shutdown hygiene: no nanobind leak reports, no refcount aborts, clean exit codes."""

import os
import subprocess
import sys
import textwrap

import pytest

import sqlanalyzer


def run_script(code: str, tmp_path) -> subprocess.CompletedProcess:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen",
           "SQLANALYZER_SETTINGS_FILE": str(tmp_path / "settings.json")}
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                          timeout=120, env=env, cwd=str(tmp_path))


def test_release_duckdb_keeps_exit_quiet(tmp_path):
    proc = run_script("""
        import atexit
        from sqlalchemy import create_engine, text
        from sqlanalyzer.db import release_duckdb
        atexit.register(release_duckdb)
        for _ in range(3):
            engine = create_engine("duckdb:///:memory:")
            with engine.connect() as conn:
                assert conn.execute(text("select 41 + 1")).scalar() == 42
            engine.dispose()
        print("ok")
    """, tmp_path)
    assert proc.returncode == 0 and "ok" in proc.stdout, proc.stderr
    assert "nanobind" not in proc.stderr and "Fatal" not in proc.stderr


def test_gui_session_with_duckdb_explain_exits_cleanly(tmp_path, duckdb_file):
    proc = run_script(f"""
        import atexit, sys
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication
        import sqlanalyzer
        from sqlanalyzer.db import ConnectionConfig, release_duckdb
        from sqlanalyzer.dialects import Engine
        from sqlanalyzer.gui.main_window import MainWindow
        atexit.register(release_duckdb)
        app = QApplication([])
        window = MainWindow()
        window.show()
        window.session.save_connection(ConnectionConfig(Engine.DUCKDB, database={str(duckdb_file)!r}))
        window.editor.setPlainText("SELECT a FROM t WHERE b = 1 OR b = 2")
        window.analyze()
        polls = 0
        def poll():
            global polls
            polls += 1
            if polls == 5:
                window.explain(False)
            if window.analysis.plan is not None or polls > 100:
                app.quit()
            else:
                QTimer.singleShot(50, poll)
        QTimer.singleShot(50, poll)
        code = app.exec()
        ok = window.analysis.plan is not None and not window.analysis.plan.logical
        sqlanalyzer.shutdown(app, window)
        del window
        print("plan loaded:", ok)
        sys.exit(code)
    """, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert "plan loaded: True" in proc.stdout
    assert "nanobind" not in proc.stderr and "Fatal" not in proc.stderr


@pytest.fixture
def duckdb_file(tmp_path):
    import duckdb

    path = tmp_path / "t.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE t AS SELECT range AS a, range % 3 AS b FROM range(1000)")
    con.close()
    return path


def test_runtime_check_flags_old_interpreters(monkeypatch):
    assert sqlanalyzer.check_runtime() is None
    monkeypatch.setattr(sqlanalyzer.sys, "version_info", (3, 11, 9, "final", 0))
    assert "Python 3.12" in sqlanalyzer.check_runtime()
