"""Packaging invariants: version consistency, console entry points, package data and the
``--version`` / ``--self-check`` switches CI runs against the built wheel and the frozen bundle."""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import sqlanalyzer

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"


@pytest.fixture(scope="module")
def project() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_module_version_matches_pyproject(project):
    assert sqlanalyzer.__version__ == project["project"]["version"]


def test_console_scripts_point_at_real_callables(project):
    import importlib

    for name, target in project["project"]["scripts"].items():
        module, attr = target.split(":")
        assert callable(getattr(importlib.import_module(module), attr)), name


def test_requires_python_matches_the_runtime_check(project):
    major, minor = sqlanalyzer.MIN_PYTHON
    assert project["project"]["requires-python"] == f">={major}.{minor}"


def test_bundled_samples_are_package_data():
    from sqlanalyzer.samples import sample_files

    files = list(sample_files())
    assert len(files) >= 30
    # they live inside the package, so a wheel built from src/ carries them
    assert all(Path(f).resolve().is_relative_to(Path(sqlanalyzer.__file__).resolve().parent) for f in files)


def test_version_flag_prints_and_exits_cleanly():
    out = subprocess.run([sys.executable, "-m", "sqlanalyzer", "--version"], capture_output=True, text=True,
                         timeout=120)
    assert out.returncode == 0 and out.stdout.strip() == f"sqlanalyzer {sqlanalyzer.__version__}"


def test_main_version_in_process(capsys):
    sqlanalyzer.main(["--version"])
    assert capsys.readouterr().out.strip() == f"sqlanalyzer {sqlanalyzer.__version__}"


def test_self_check_passes_in_a_subprocess(tmp_path):
    env = {"QT_QPA_PLATFORM": "offscreen", "SQLANALYZER_SETTINGS_FILE": str(tmp_path / "s.json"),
           "SQLANALYZER_HISTORY_FILE": str(tmp_path / "h.json")}
    import os

    out = subprocess.run([sys.executable, "-m", "sqlanalyzer", "--self-check"], capture_output=True, text=True,
                         timeout=300, env={**os.environ, **env})
    assert out.returncode == 0, out.stderr
    assert "self-check: ok" in out.stdout


def test_self_check_reports_problems_instead_of_raising(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def broken(name, *args, **kwargs):
        if name == "graphviz":
            raise ImportError("simulated missing module")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken)
    problems = sqlanalyzer.self_check()
    assert any(p.startswith("graphviz: ImportError") for p in problems)


def test_self_check_exit_code_reflects_problems(monkeypatch, capsys):
    monkeypatch.setattr(sqlanalyzer, "self_check", lambda: ["x: broken"])
    with pytest.raises(SystemExit) as exc:
        sqlanalyzer.main(["--self-check"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "x: broken" in captured.err and "FAILED" in captured.out


def test_pyinstaller_spec_names_every_dynamic_import_family():
    spec = (ROOT / "packaging" / "sqlanalyzer.spec").read_text(encoding="utf-8")
    for needle in ("duckdb_engine", "trino.sqlalchemy", "snowflake.sqlalchemy", "psycopg2", "sqlglot.dialects",
                   "keyring.backends", 'collect_data_files("sqlanalyzer")', "launcher.py"):
        assert needle in spec, needle


def test_ci_workflows_are_well_formed_yaml():
    yaml = pytest.importorskip("yaml")
    for name in ("ci.yml", "release.yml"):
        doc = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
        assert "jobs" in doc and doc["jobs"], name
        for job in doc["jobs"].values():
            assert "runs-on" in job and "steps" in job
