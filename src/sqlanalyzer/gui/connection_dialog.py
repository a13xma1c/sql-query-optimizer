"""Dialog for editing the database connection of the current engine."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..db import ConnectionConfig, test_connection
from ..dialects import ENGINES, Engine
from .workers import run_async

_LABELS = {
    Engine.POSTGRES: dict(host="Host", database="Database", schema="", extras="e.g. sslmode=require"),
    Engine.DUCKDB: dict(host="", database="Database file (blank = in-memory)", schema="", extras=""),
    Engine.TRINO: dict(host="Host", database="Catalog", schema="Schema", extras="e.g. http_scheme=https"),
    Engine.SNOWFLAKE: dict(host="Account (e.g. xy12345.eu-west-1)", database="Database", schema="Schema",
                           extras="warehouse=WH\nrole=ANALYST"),
}


class ConnectionDialog(QDialog):
    def __init__(self, engine: Engine, existing: ConnectionConfig | None, parent=None) -> None:
        super().__init__(parent)
        self.engine = engine
        info = ENGINES[engine]
        self.setWindowTitle(f"{info.label} connection")
        self.setMinimumWidth(460)
        labels = _LABELS[engine]
        cfg = existing or ConnectionConfig(engine)

        self.host = QLineEdit(cfg.host)
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("default")
        self.port.setValue(cfg.port or 0)
        self.database = QLineEdit(cfg.database)
        self.schema = QLineEdit(cfg.schema)
        self.username = QLineEdit(cfg.username)
        self.password = QLineEdit(cfg.password)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.extras = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in cfg.extras.items()))
        self.extras.setPlaceholderText(labels["extras"])
        self.extras.setFixedHeight(64)

        form = QFormLayout()
        if labels["host"]:
            form.addRow(labels["host"], self.host)
            if info.default_port:
                form.addRow("Port", self.port)
        db_row = QHBoxLayout()
        db_row.addWidget(self.database)
        if engine is Engine.DUCKDB:
            browse = QPushButton("Browse…")
            browse.clicked.connect(self._browse)
            db_row.addWidget(browse)
        form.addRow(labels["database"], db_row)
        if labels["schema"]:
            form.addRow(labels["schema"], self.schema)
        if engine is not Engine.DUCKDB:
            form.addRow("User", self.username)
            form.addRow("Password", self.password)
            form.addRow("Extra parameters", self.extras)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        test = QPushButton("Test connection")
        test.clicked.connect(self._test)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        note = QLabel("The password is kept in memory for this session only and is never saved to disk.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        row = QHBoxLayout()
        row.addWidget(test)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.status)
        layout.addLayout(row)

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "DuckDB database", "", "DuckDB (*.duckdb *.db);;All files (*)")
        if path:
            self.database.setText(path)

    def config(self) -> ConnectionConfig:
        extras = {}
        for line in self.extras.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                extras[k.strip()] = v.strip()
        return ConnectionConfig(self.engine, self.host.text().strip(), self.port.value() or None,
                                self.database.text().strip(), self.schema.text().strip(),
                                self.username.text().strip(), self.password.text(), extras)

    def _test(self) -> None:
        self.status.setText("Connecting…")
        cfg = self.config()

        def ok(msg) -> None:
            self.status.setStyleSheet("color: #1a7f37;")
            self.status.setText(str(msg))

        def fail(msg: str) -> None:
            self.status.setStyleSheet("color: #d73027;")
            self.status.setText(msg)

        run_async(self, lambda: test_connection(cfg), ok, fail)

