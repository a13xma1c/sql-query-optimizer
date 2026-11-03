"""Dialog to paste an existing EXPLAIN output."""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QVBoxLayout

from ..dialects import ENGINES, Engine


class PastePlanDialog(QDialog):
    def __init__(self, engine: Engine, current: str = "", parent=None) -> None:
        super().__init__(parent)
        info = ENGINES[engine]
        self.setWindowTitle(f"Paste {info.label} plan")
        self.resize(720, 460)
        self.text = QPlainTextEdit(current)
        self.text.setPlaceholderText(f"Output of {info.plan_format}")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Paste the output of <code>{info.plan_format}</code>:"))
        layout.addWidget(self.text, 1)
        layout.addWidget(buttons)

    def plan_text(self) -> str:
        return self.text.toPlainText().strip()
