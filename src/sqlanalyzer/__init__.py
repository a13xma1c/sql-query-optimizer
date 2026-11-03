"""SQL Analyzer: validate, lint and understand the performance of SQL queries."""

from __future__ import annotations

import sys


def main() -> None:
    from PySide6.QtWidgets import QApplication

    from .gui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setOrganizationName("SQLAnalyzer")
    app.setApplicationName("SQLAnalyzer")
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
