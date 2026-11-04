import atexit
import os
from pathlib import Path

import pytest

from sqlanalyzer.db import release_duckdb

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# see release_duckdb: keeps DuckDB's nanobind teardown quiet and deterministic at interpreter exit
atexit.register(release_duckdb)

DATA = Path(__file__).parent / "data"


def read(name: str) -> str:
    return (DATA / name).read_text(encoding="utf-8")


def _flush_deferred_deletes() -> None:
    """Actually destroy widgets that pytest-qt closed with ``deleteLater()``.

    Without a running event loop those deletions stay queued until the QApplication is torn down at
    interpreter exit. Destroying widgets that late runs Python cleanup in the middle of interpreter
    finalisation, which makes DuckDB's nanobind module report its still-referenced objects as leaked
    ("nanobind: leaked N instances"). Processing the queue right away keeps teardown deterministic.
    """
    try:
        from PySide6.QtCore import QCoreApplication, QEvent
    except ImportError:  # pragma: no cover - GUI extras not installed
        return
    app = QCoreApplication.instance()
    if app is None:
        return
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.hookimpl(trylast=True)
def pytest_runtest_teardown(item, nextitem):
    # runs inside pytest-qt's teardown wrapper (after it closed and deleteLater()'d the widgets) and after
    # the fixture finalizers, which may still need to look at the widgets
    _flush_deferred_deletes()


def pytest_sessionfinish(session, exitstatus):
    """Tear down Qt state deterministically before the interpreter starts finalising modules."""
    import gc

    try:
        from PySide6.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(10_000)
    except ImportError:  # pragma: no cover
        pass
    _flush_deferred_deletes()
    for _ in range(3):  # collect reference cycles now (SQLAlchemy pools, Qt wrappers) instead of at exit
        gc.collect()
    _flush_deferred_deletes()



@pytest.fixture(scope="session")
def pg_server_or_skip(tmp_path_factory):
    """A private throw-away PostgreSQL (unit tests that need a live server); skipped when unavailable."""
    import os

    from e2e.conftest import ENV_URLS, TemporaryPostgres
    from sqlanalyzer.db import ConnectionConfig
    from sqlanalyzer.dialects import Engine

    url = os.environ.get(ENV_URLS[Engine.POSTGRES])
    if url:
        yield ConnectionConfig.from_url(Engine.POSTGRES, url)
        return
    if not TemporaryPostgres.available():
        pytest.skip("no PostgreSQL available")
    server = TemporaryPostgres(tmp_path_factory.mktemp("pg-unit"))
    try:
        server.start()
        yield server.cfg
    finally:
        server.stop()
