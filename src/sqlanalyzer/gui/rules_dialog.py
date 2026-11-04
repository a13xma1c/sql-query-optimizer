"""Enable/disable individual *finding* rules (lint, join, scan, partition, engine, schema, plan).

Mirrors :class:`sqlanalyzer.gui.optimize_tab.RulesDialog`, which does the same thing for the
optimizer's rewrite rules; this one is for the checks that simply report a finding, filterable by
category (and, for engine-specific checks, restricted to the engines they apply to).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..models import Category
from ..rules_registry import ALL_RULES, RuleInfo
from .geometry import fit_dialog


class FindingRulesDialog(QDialog):
    def __init__(self, disabled: set[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Finding rules")
        fit_dialog(self, 640, 560)
        self._boxes: dict[str, QCheckBox] = {}
        self._rows: list[tuple[RuleInfo, QCheckBox, QLabel]] = []

        self.filter_box = QComboBox()
        self.filter_box.addItem("All categories", None)
        for cat in Category:
            self.filter_box.addItem(cat.value.capitalize(), cat)
        self.filter_box.currentIndexChanged.connect(self._apply_filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name…")
        self.search.textChanged.connect(self._apply_filter)
        top = QHBoxLayout()
        top.addWidget(QLabel("Category:"))
        top.addWidget(self.filter_box)
        top.addWidget(self.search, 1)

        inner = QWidget()
        self._form = QVBoxLayout(inner)
        for rule in ALL_RULES:
            box = QCheckBox(f"{rule.name}  [{rule.category.value}]")
            box.setChecked(rule.name not in disabled)
            label = QLabel(rule.description + self._engines_note(rule))
            label.setWordWrap(True)
            label.setStyleSheet("color: gray; margin-left: 24px; margin-bottom: 6px;")
            self._form.addWidget(box)
            self._form.addWidget(label)
            self._boxes[rule.name] = box
            self._rows.append((rule, box, label))
        self._form.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)

    @staticmethod
    def _engines_note(rule: RuleInfo) -> str:
        from ..dialects import ENGINES, Engine

        if rule.engines == frozenset(Engine):
            return ""
        names = ", ".join(ENGINES[e].label for e in rule.engines)
        return f" (only {names})"

    def _apply_filter(self) -> None:
        category = self.filter_box.currentData()
        needle = self.search.text().strip().lower()
        for rule, box, label in self._rows:
            visible = (category is None or rule.category == category) and (needle in rule.name.lower())
            box.setVisible(visible)
            label.setVisible(visible)

    def disabled_rules(self) -> set[str]:
        return {name for name, box in self._boxes.items() if not box.isChecked()}
