"""Complexity score and metric breakdown."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QLabel,
    QProgressBar,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..complexity import ComplexityReport

_LEVEL_COLOR = {"Low": "#1a7f37", "Moderate": "#bf8700", "High": "#d9622b", "Very high": "#d73027"}
_NAMES = {
    "joins": "Joins", "subqueries": "Subqueries", "max_nesting_depth": "Max SELECT nesting",
    "ctes": "CTEs", "set_operations": "Set operations (UNION/…)", "window_functions": "Window functions",
    "aggregations": "Aggregate calls", "predicates": "Filter predicates", "case_expressions": "CASE expressions",
    "distinct_tables": "Distinct tables", "correlated_subqueries": "Correlated subqueries",
    "statements": "Statements",
}


class ComplexityTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.level = QLabel("No analysis yet.")
        self.level.setStyleSheet("font-size: 16px; font-weight: 600;")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Metric", "Count", "Score contribution"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout = QVBoxLayout(self)
        layout.addWidget(self.level)
        layout.addWidget(self.bar)
        layout.addWidget(self.table, 1)

    def set_report(self, report: ComplexityReport | None) -> None:
        self.table.setRowCount(0)
        if report is None:
            self.level.setText("Complexity is available once the SQL parses.")
            self.level.setStyleSheet("font-size: 16px; font-weight: 600;")
            self.bar.setValue(0)
            return
        color = _LEVEL_COLOR[report.level]
        self.level.setText(f"{report.level} complexity — {report.score}/100")
        self.level.setStyleSheet(f"font-size: 16px; font-weight: 600; color: {color};")
        self.bar.setValue(report.score)
        self.bar.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; }}")
        rows = sorted(report.metrics.items(), key=lambda kv: -report.contributions.get(kv[0], -1))
        self.table.setRowCount(len(rows))
        for r, (key, value) in enumerate(rows):
            contrib = report.contributions.get(key)
            for c, text in enumerate((_NAMES.get(key, key), str(value), f"+{contrib:.1f}" if contrib else "")):
                self.table.setItem(r, c, QTableWidgetItem(text))
