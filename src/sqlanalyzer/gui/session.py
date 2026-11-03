"""State shared by all tabs: selected engine, connections and partition hints."""

from __future__ import annotations

from PySide6.QtCore import QObject, QSettings, Signal

from ..db import ConnectionConfig
from ..dialects import Engine
from ..optimizer import OPT_IN_RULES, SchemaHints, parse_schema
from ..partition import parse_hints


class Session(QObject):
    engine_changed = Signal(object)
    optimizer_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.engine = Engine.DUCKDB
        self.connections: dict[Engine, ConnectionConfig] = {}
        self.hints_text = ""
        self.schema_text = ""
        self.include_review = True
        self.disabled_rules: set[str] = set(OPT_IN_RULES)
        self._load()

    def set_engine(self, engine: Engine | str) -> None:
        engine = Engine(engine)  # Qt item data round-trips enums as plain strings
        if engine != self.engine:
            self.engine = engine
            self.engine_changed.emit(engine)

    @property
    def connection(self) -> ConnectionConfig | None:
        return self.connections.get(self.engine)

    @property
    def hints(self) -> dict[str, list[str]]:
        return parse_hints(self.hints_text)

    @property
    def schema_hints(self) -> SchemaHints:
        return parse_schema(self.schema_text)

    @property
    def schema(self) -> dict | None:
        return self.schema_hints.mapping()

    # Passwords are intentionally never persisted.
    def save_connection(self, cfg: ConnectionConfig) -> None:
        self.connections[cfg.engine] = cfg
        s = QSettings()
        s.beginGroup(f"connection/{cfg.engine.value}")
        for key in ("host", "database", "schema", "username"):
            s.setValue(key, getattr(cfg, key))
        s.setValue("port", cfg.port or 0)
        s.setValue("extras", dict(cfg.extras))
        s.endGroup()

    def save_hints(self, text: str) -> None:
        self.hints_text = text
        QSettings().setValue("partition_hints", text)

    def save_schema(self, text: str) -> None:
        self.schema_text = text
        QSettings().setValue("schema_hints", text)

    def save_optimizer(self, include_review: bool, disabled_rules: set[str]) -> None:
        changed = (include_review, set(disabled_rules)) != (self.include_review, self.disabled_rules)
        self.include_review = include_review
        self.disabled_rules = set(disabled_rules)
        s = QSettings()
        s.setValue("optimizer/include_review", include_review)
        s.setValue("optimizer/disabled_rules", sorted(self.disabled_rules))
        if changed:
            self.optimizer_changed.emit()

    def _load(self) -> None:
        s = QSettings()
        self.hints_text = s.value("partition_hints", "", str)
        self.schema_text = s.value("schema_hints", "", str)
        self.include_review = s.value("optimizer/include_review", True, bool)
        if s.contains("optimizer/disabled_rules"):
            raw = s.value("optimizer/disabled_rules", [])
            if isinstance(raw, str):
                raw = [raw] if raw else []
            self.disabled_rules = {str(r) for r in raw}
        for engine in Engine:
            if s.contains(f"connection/{engine.value}/host") or s.contains(f"connection/{engine.value}/database"):
                s.beginGroup(f"connection/{engine.value}")
                extras = s.value("extras", {}) or {}
                self.connections[engine] = ConnectionConfig(
                    engine, s.value("host", "", str), int(s.value("port", 0) or 0) or None,
                    s.value("database", "", str), s.value("schema", "", str), s.value("username", "", str),
                    "", {str(k): str(v) for k, v in dict(extras).items()})
                s.endGroup()
