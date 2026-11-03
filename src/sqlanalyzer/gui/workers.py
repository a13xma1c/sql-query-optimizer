"""Run blocking work (database round trips) off the GUI thread."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import shiboken6
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn: Callable[[], Any], signals: _Signals) -> None:
        super().__init__()
        self._fn, self._signals = fn, signals

    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            self._emit("failed", str(exc) or exc.__class__.__name__)
        else:
            self._emit("done", result)

    def _emit(self, name: str, value: Any) -> None:
        """Emit unless the receiver (and its parent window) was destroyed while the task ran."""
        try:
            if shiboken6.isValid(self._signals):
                getattr(self._signals, name).emit(value)
        except RuntimeError:  # C++ object deleted between the check and the emit
            pass


def run_async(parent: QObject, fn: Callable[[], Any], on_done: Callable[[Any], None],
              on_failed: Callable[[str], None]) -> None:
    """Execute ``fn`` in the global thread pool; callbacks run on the GUI thread."""
    signals = _Signals(parent)
    signals.done.connect(on_done)
    signals.failed.connect(on_failed)
    QThreadPool.globalInstance().start(_Task(fn, signals))
