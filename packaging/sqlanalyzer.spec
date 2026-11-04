# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the SQL Analyzer desktop bundle.

Build from the repository root::

    uv run --group packaging pyinstaller packaging/sqlanalyzer.spec --noconfirm

Produces ``dist/sqlanalyzer/`` (one-folder bundle; ``sqlanalyzer.app`` on macOS). The Graphviz
``dot`` binary is *not* bundled - it stays a runtime dependency, exactly as for the pip install.

Everything the application imports dynamically has to be named here, because PyInstaller only
follows static imports: the SQLAlchemy dialect plugins are loaded by entry point, SQLGlot's
dialects by name, keyring's backends by entry point. ``sqlanalyzer --self-check`` on the result
verifies all of it (CI does that after every build).
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller

hiddenimports = [
    # SQLAlchemy dialects (loaded via entry points / dotted names at connect time)
    *collect_submodules("duckdb_engine"),
    *collect_submodules("trino.sqlalchemy"),
    *collect_submodules("snowflake.sqlalchemy"),
    "psycopg2",
    "sqlalchemy.dialects.postgresql",
    "sqlalchemy.dialects.postgresql.psycopg2",
    # SQLGlot picks dialect modules by name
    *collect_submodules("sqlglot.dialects"),
    *collect_submodules("sqlglot.optimizer"),
    # keyring discovers its backends through entry points
    *collect_submodules("keyring.backends"),
    # Qt modules imported lazily in a few places
    "PySide6.QtSvg",
    "PySide6.QtSvgWidgets",
]

datas = [
    *collect_data_files("sqlanalyzer"),  # samples/*.sql
    *collect_data_files("sqlglot"),
    *collect_data_files("snowflake.connector"),
    # entry-point metadata the plugin loaders (SQLAlchemy, keyring) read at runtime
    *copy_metadata("sqlanalyzer"),
    *copy_metadata("duckdb_engine"),
    *copy_metadata("trino"),
    *copy_metadata("snowflake-sqlalchemy"),
    *copy_metadata("snowflake-connector-python"),
    *copy_metadata("keyring"),
    *copy_metadata("sqlalchemy"),
]

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # heavy optional pulls nothing in the app uses
        "tkinter", "matplotlib", "numpy.testing", "IPython", "pytest",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick",
        "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="sqlanalyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # Windowed on macOS/Windows (no console flashing up behind the GUI). `--self-check` still works
    # there: Python's print() is a no-op when the bootloader detached stdout, and CI reads the exit code.
    console=sys.platform == "linux",
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="sqlanalyzer",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="SQL Analyzer.app",
        bundle_identifier="io.sqlanalyzer.app",
        info_plist={
            "CFBundleShortVersionString": "0.1.0",
            "NSHighResolutionCapable": True,
        },
    )
