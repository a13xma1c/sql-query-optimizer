"""Import the schema (columns, keys, indexes, statistics, partition keys) from the connected database."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from ..catalog import CatalogOptions, CatalogResult, introspect
from ..db import ConnectionConfig
from ..dialects import ENGINES
from .workers import run_async


class SchemaImportDialog(QDialog):
    """Runs the introspection off the GUI thread; ``imported`` holds the outcome after *Import* (not ``result``, which is ``QDialog.result()``)."""

    def __init__(self, cfg: ConnectionConfig, referenced_tables: list[str], parent=None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self.imported: CatalogResult | None = None
        self.setWindowTitle(f"Import schema from {ENGINES[cfg.engine].label}")
        self.setMinimumWidth(520)

        self.only_referenced = QCheckBox(
            f"Only the {len(referenced_tables)} table(s) used by the current query"
            + (f" ({', '.join(referenced_tables[:6])}{'…' if len(referenced_tables) > 6 else ''})"
               if referenced_tables else ""))
        self.only_referenced.setChecked(bool(referenced_tables))
        self.only_referenced.setEnabled(bool(referenced_tables))
        self.schemas = QLineEdit(cfg.schema or "")
        self.schemas.setPlaceholderText("schema names, comma separated (blank = the connection's default)")
        self.distinct = QCheckBox("Collect approximate distinct counts per column (scans each table)")
        self.replace = QRadioButton("Replace the current schema hints")
        self.merge = QRadioButton("Merge: imported tables override, others are kept")
        self.merge.setChecked(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.import_btn = QPushButton("Import")
        self.import_btn.clicked.connect(self._run)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.rejected.connect(self.reject)
        buttons.addButton(self.import_btn, QDialogButtonBox.ButtonRole.AcceptRole)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Connection: <code>{cfg.display()}</code>"))
        layout.addWidget(self.only_referenced)
        layout.addWidget(QLabel("Schemas:"))
        layout.addWidget(self.schemas)
        layout.addWidget(self.distinct)
        layout.addWidget(self.replace)
        layout.addWidget(self.merge)
        layout.addWidget(self.status)
        layout.addWidget(buttons)
        self._referenced = referenced_tables

    def options(self) -> CatalogOptions:
        schemas = [s.strip() for s in self.schemas.text().split(",") if s.strip()] or None
        return CatalogOptions(tables=self._referenced if self.only_referenced.isChecked() else None,
                              schemas=schemas, distinct_counts=self.distinct.isChecked())

    @property
    def merge_mode(self) -> bool:
        return self.merge.isChecked()

    def _run(self) -> None:
        self.imported = None
        self.import_btn.setEnabled(False)
        self.status.setStyleSheet("color: gray;")
        self.status.setText("Reading the catalog…")
        options = self.options()

        def done(result: CatalogResult) -> None:
            self.imported = result
            if not result.hints.tables:
                self.import_btn.setEnabled(True)
                self.status.setStyleSheet("color: #d73027;")
                self.status.setText("No tables found. Check the schema name"
                                    + (" or untick 'only referenced tables'." if options.tables else "."))
                return
            self.status.setStyleSheet("color: #1a7f37;")
            self.status.setText(result.summary())
            self.accept()

        def failed(msg: str) -> None:
            self.import_btn.setEnabled(True)
            self.status.setStyleSheet("color: #d73027;")
            self.status.setText(msg)

        run_async(self, lambda: introspect(self.cfg, options), done, failed)
