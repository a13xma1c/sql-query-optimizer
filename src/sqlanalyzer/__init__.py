"""SQL Analyzer: validate, lint and understand the performance of SQL queries."""

from __future__ import annotations

import sys

__version__ = "0.1.0"  # kept in sync with pyproject.toml (tests/test_packaging.py checks)
MIN_PYTHON = (3, 12)


def check_runtime() -> str | None:
    """Return a message if this interpreter cannot run the GUI reliably.

    PySide6's ``abi3`` wheels (6.12+) hand out *borrowed* references to ``None``
    and ``True`` from void methods and ``Signal.emit``. That is harmless on
    Python 3.12+, where those singletons are immortal, but on 3.11 the reference
    counts really drop and the process aborts with ``none_dealloc`` /
    ``bool_dealloc`` after a few thousand widget calls.
    """
    if sys.version_info < MIN_PYTHON:
        return (f"SQL Analyzer needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer; this is "
                f"{sys.version.split()[0]}. PySide6 6.12 aborts on older interpreters after a few thousand "
                "widget calls (borrowed None/True references). Run it with 'uv run' or a 3.12+ interpreter.")
    return None


def self_check() -> list[str]:
    """Import every runtime dependency a packaged build must ship; returns the problems found.

    Frozen bundles (PyInstaller) can silently miss dynamically imported modules - SQLAlchemy
    dialect plugins, keyring backends, Qt platform plugins - so CI runs ``sqlanalyzer --self-check``
    on the built bundle instead of trusting that it merely starts.
    """
    problems: list[str] = []
    modules = [
        "sqlglot", "sqlalchemy", "duckdb", "duckdb_engine", "psycopg2", "trino.sqlalchemy",
        "snowflake.sqlalchemy", "graphviz", "keyring", "PySide6.QtWidgets", "PySide6.QtSvgWidgets",
        "sqlanalyzer.gui.main_window", "sqlanalyzer.analyzer", "sqlanalyzer.demo",
    ]
    for name in modules:
        try:
            __import__(name)
        except Exception as exc:  # noqa: BLE001 - report every failure, not just the first
            problems.append(f"{name}: {type(exc).__name__}: {exc}")
    try:
        from .samples import load_samples

        if not load_samples():
            problems.append("samples: no bundled sample queries found (package data missing)")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"samples: {type(exc).__name__}: {exc}")
    try:
        from .analyzer import analyze_query
        from .dialects import Engine

        analysis = analyze_query("SELECT * FROM orders o, customers c WHERE o.customer_id = c.id", Engine.DUCKDB)
        if not analysis.syntax_ok or not analysis.findings:
            problems.append("analysis: the sample query produced no findings")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"analysis: {type(exc).__name__}: {exc}")
    if not problems:
        problems += _gui_self_check()
    return problems


def _gui_self_check() -> list[str]:
    """Build the main window off-screen (Qt platform plugin, SVG module, widgets) and tear it down."""
    import os
    import tempfile

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    with tempfile.TemporaryDirectory() as tmp:  # never touch the user's real settings/history
        os.environ.setdefault("SQLANALYZER_SETTINGS_FILE", os.path.join(tmp, "settings.json"))
        os.environ.setdefault("SQLANALYZER_HISTORY_FILE", os.path.join(tmp, "history.json"))
        try:
            from PySide6.QtWidgets import QApplication

            from .gui.main_window import MainWindow

            app = QApplication.instance() or QApplication([])
            window = MainWindow()
            window.show()
            app.processEvents()
            ok = window.analysis is not None and window.analysis.syntax_ok
            shutdown(app, window)
            return [] if ok else ["gui: the main window did not analyze its sample query"]
        except Exception as exc:  # noqa: BLE001
            return [f"gui: {type(exc).__name__}: {exc}"]


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--version" in args or "-V" in args:
        print(f"sqlanalyzer {__version__}")
        return
    if "--self-check" in args:
        problems = self_check()
        for problem in problems:
            print(problem, file=sys.stderr)
        print("self-check: " + ("FAILED" if problems else "ok"))
        sys.exit(1 if problems else 0)
    runtime_problem = check_runtime()
    if runtime_problem:
        sys.exit(runtime_problem)
    import atexit

    from PySide6.QtWidgets import QApplication

    from .db import release_duckdb
    from .gui.main_window import MainWindow

    atexit.register(release_duckdb)

    app = QApplication(sys.argv)
    app.setOrganizationName("SQLAnalyzer")
    app.setApplicationName("SQLAnalyzer")
    window = MainWindow()
    window.show()
    code = app.exec()
    shutdown(app, window)
    sys.exit(code)


def shutdown(app, window) -> None:
    """Destroy the GUI in a controlled order before the interpreter finalises.

    Widgets that are still alive when Python tears down its modules get destroyed
    in the middle of that process; DuckDB's bindings then report its module-level
    objects as leaked (``nanobind: leaked N instances``). Deleting the window,
    draining the worker pool and collecting garbage first keeps the exit quiet
    and deterministic.
    """
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent, QThreadPool

    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QThreadPool.globalInstance().waitForDone(5_000)
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
