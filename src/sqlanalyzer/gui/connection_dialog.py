"""Dialog for editing the database connection of the current engine."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..db import ConnectionConfig, duckdb_storage_version, test_connection
from ..demo import demo_path, populate_duckdb
from ..dialects import ENGINES, Engine
from .geometry import available_rect
from .profiles import ProfileStore, keyring_available
from .workers import run_async

DUCKDB_FILTER = "DuckDB databases (*.duckdb *.db *.ddb);;All files (*)"

_LABELS = {
    Engine.POSTGRES: dict(host="Host", database="Database", schema="", extras="e.g. sslmode=require"),
    Engine.DUCKDB: dict(host="", database="Database file (blank = in-memory)", schema="", extras=""),
    Engine.TRINO: dict(host="Host", database="Catalog", schema="Schema", extras="e.g. http_scheme=https"),
    Engine.SNOWFLAKE: dict(host="Account (e.g. xy12345.eu-west-1)", database="Database", schema="Schema",
                           extras="warehouse=WH\nrole=ANALYST"),
}


class ConnectionDialog(QDialog):
    def __init__(self, engine: Engine, existing: ConnectionConfig | None, parent=None,
                 profiles: ProfileStore | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self.profiles = profiles
        info = ENGINES[engine]
        self.setWindowTitle(f"{info.label} connection")
        self.setMinimumWidth(min(720 if engine is Engine.DUCKDB else 460, available_rect(parent).width() - 40))
        labels = _LABELS[engine]
        cfg = existing or ConnectionConfig(engine)

        self.profile_box = QComboBox()
        self.save_profile_btn = QPushButton("Save as profile…")
        self.delete_profile_btn = QPushButton("Delete profile")
        if self.profiles is not None and engine is not Engine.DUCKDB:
            self._reload_profiles()
            self.profile_box.currentIndexChanged.connect(self._profile_selected)
            self.save_profile_btn.clicked.connect(self._save_profile)
            self.delete_profile_btn.clicked.connect(self._delete_profile)
            profile_row = QHBoxLayout()
            profile_row.addWidget(QLabel("Profile:"))
            profile_row.addWidget(self.profile_box, 1)
            profile_row.addWidget(self.save_profile_btn)
            profile_row.addWidget(self.delete_profile_btn)

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
        self.file_status = QLabel("")
        self.file_status.setStyleSheet("color: gray;")
        if engine is Engine.DUCKDB:
            open_btn = QPushButton("Open…")
            open_btn.setToolTip("Select an existing DuckDB database file")
            open_btn.clicked.connect(self._open_existing)
            new_btn = QPushButton("New…")
            new_btn.setToolTip("Choose where a new database file should be created")
            new_btn.clicked.connect(self._new_file)
            self.demo_btn = QPushButton("Use demo database")
            self.demo_btn.setToolTip("Create (or reuse) the bundled demo database: orders, customers, products, "
                                     "shipments, events … with a few hundred thousand rows")
            self.demo_btn.clicked.connect(self._use_demo)
            for b in (open_btn, new_btn, self.demo_btn):
                db_row.addWidget(b)
            self.database.textChanged.connect(self._refresh_file_status)
        form.addRow(labels["database"], db_row)
        if engine is Engine.DUCKDB:
            form.addRow("", self.file_status)
            self._refresh_file_status()
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
        note = QLabel(self._password_note())
        note.setWordWrap(True)
        note.setStyleSheet("color: gray;")

        row = QHBoxLayout()
        row.addWidget(test)
        row.addStretch(1)
        row.addWidget(buttons)
        layout = QVBoxLayout(self)
        if self.profiles is not None and engine is not Engine.DUCKDB:
            layout.addLayout(profile_row)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.status)
        layout.addLayout(row)

    @staticmethod
    def _password_note() -> str:
        if keyring_available():
            return ("Saving a password as part of a profile stores it in your OS keychain, not in this "
                   "application's settings file. Typing one here without saving a profile keeps it in "
                   "memory for this session only.")
        return ("No OS keychain is available here, so passwords (including those of saved profiles) are "
               "kept in memory for this session only and are never written to disk.")

    # ------------------------------------------------------------------- profiles
    @property
    def _store(self) -> ProfileStore:
        """The profile store; the profile controls are only wired up when one was given."""
        assert self.profiles is not None
        return self.profiles

    def _reload_profiles(self, select: str | None = None) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.clear()
        self.profile_box.addItem("(none)", None)
        for pid, profile in sorted(self._store.profiles.items(), key=lambda kv: kv[1].name.lower()):
            self.profile_box.addItem(profile.name, pid)
        if select is not None:
            self.profile_box.setCurrentIndex(self.profile_box.findData(select))
        self.profile_box.blockSignals(False)
        self.delete_profile_btn.setEnabled(self.profile_box.currentData() is not None)

    def _profile_selected(self) -> None:
        pid = self.profile_box.currentData()
        self.delete_profile_btn.setEnabled(pid is not None)
        if pid is None:
            return
        cfg = self._store.resolve(pid)
        self.host.setText(cfg.host)
        self.port.setValue(cfg.port or 0)
        self.database.setText(cfg.database)
        self.schema.setText(cfg.schema)
        self.username.setText(cfg.username)
        self.password.setText(cfg.password)
        self.extras.setPlainText("\n".join(f"{k}={v}" for k, v in cfg.extras.items()))

    def _save_profile(self) -> None:
        current = self.profile_box.currentData()
        default_name = next((p.name for p in self._store.profiles.values() if p.id == current), "")
        name, ok = QInputDialog.getText(self, "Save profile", "Profile name:", text=default_name)
        if not ok or not name.strip():
            return
        profile = self._store.save(name.strip(), self.config(), self.password.text(), profile_id=current)
        self._reload_profiles(select=profile.id)
        if self.password.text() and not profile.password_saved:
            QMessageBox.information(self, "Password not saved",
                                    "No OS keychain is available, so only the non-secret parts of this "
                                    "profile were saved. You will need to type the password again next time.")

    def _delete_profile(self) -> None:
        pid = self.profile_box.currentData()
        if pid is None:
            return
        self._store.delete(pid)
        self._reload_profiles()

    # ------------------------------------------------------------- DuckDB files
    def _open_existing(self) -> None:
        start = str(Path(self.database.text()).parent) if self.database.text() else ""
        path, _ = QFileDialog.getOpenFileName(self, "Open DuckDB database", start, DUCKDB_FILTER)
        if path:
            self.database.setText(path)

    def _new_file(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "New DuckDB database", "analyzer.duckdb", DUCKDB_FILTER)
        if path:
            self.database.setText(path)

    def _use_demo(self) -> None:
        target = demo_path()
        self.demo_btn.setEnabled(False)
        self.status.setStyleSheet("color: gray;")
        self.status.setText(f"Preparing the demo database at {target} …")

        def done(path) -> None:
            self.demo_btn.setEnabled(True)
            self.database.setText(str(path))
            self.status.setStyleSheet("color: #1a7f37;")
            self.status.setText("Demo database ready. Press 'Test connection' or OK.")

        def failed(msg: str) -> None:
            self.demo_btn.setEnabled(True)
            self.status.setStyleSheet("color: #d73027;")
            self.status.setText(f"Could not create the demo database: {msg}")

        run_async(self, lambda: populate_duckdb(target, replace=False), done, failed)

    def _refresh_file_status(self) -> None:
        self.file_status.setText(describe_duckdb_path(self.database.text().strip()))

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



def describe_duckdb_path(path: str) -> str:
    """One line telling the user what the DuckDB 'database' field currently points at."""
    if not path or path == ":memory:":
        return "In-memory database: empty, nothing is persisted."
    target = Path(path)
    if target.is_dir():
        return "This is a directory - choose a file."
    if target.exists():
        size = target.stat().st_size
        unit = "MB" if size >= 1_000_000 else "KB"
        version = duckdb_storage_version(target)
        fmt = f", storage format {version[0]} = DuckDB {version[1]}" if version else ""
        return f"Existing database file ({size / (1_000_000 if unit == 'MB' else 1_000):.1f} {unit}{fmt})."
    if target.parent.exists():
        return "File does not exist yet - it will be created empty on first connection."
    return "The folder does not exist."
