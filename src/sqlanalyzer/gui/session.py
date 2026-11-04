"""State shared by all tabs: selected engine, connections, hints and optimizer/LLM preferences."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from ..db import ConnectionConfig
from ..dialects import Engine
from ..history import QueryHistory
from ..llm import LlmConfig
from ..optimizer import OPT_IN_RULES, SchemaHints, TableStats, parse_schema
from ..partition import parse_hints
from .settings import Settings
from .theme import Theme


class Session(QObject):
    engine_changed = Signal(object)
    optimizer_changed = Signal()
    llm_changed = Signal()
    theme_changed = Signal(object)

    def __init__(self, settings: Settings | None = None, history: QueryHistory | None = None) -> None:
        super().__init__()
        self.settings = settings or Settings()
        self.history = history or QueryHistory()
        self.engine = Engine.DUCKDB
        self.connections: dict[Engine, ConnectionConfig] = {}
        self.hints_text = ""
        self.schema_text = ""
        self.include_review = True
        self.disabled_rules: set[str] = set(OPT_IN_RULES)
        self.disabled_finding_rules: set[str] = set()
        self.llm = LlmConfig()  # API keys/passwords live in memory only
        self.theme = Theme.SYSTEM
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
        """Partition hints: the typed ones plus the partition keys declared/imported in the schema."""
        merged = dict(self.schema_hints.partition_hints())
        merged.update(parse_hints(self.hints_text))
        return merged

    @property
    def schema_hints(self) -> SchemaHints:
        return parse_schema(self.schema_text)

    @property
    def schema(self) -> dict | None:
        return self.schema_hints.mapping()

    @property
    def stats(self) -> dict[str, TableStats]:
        return self.schema_hints.stats

    # Passwords are intentionally never persisted.
    def save_connection(self, cfg: ConnectionConfig) -> None:
        self.connections[cfg.engine] = cfg
        s = self.settings
        prefix = f"connection/{cfg.engine.value}/"
        for key in ("host", "database", "schema", "username"):
            s.set_value(prefix + key, getattr(cfg, key))
        s.set_value(prefix + "port", cfg.port or 0)
        s.set_value(prefix + "extras", dict(cfg.extras))

    def save_llm(self, config: LlmConfig) -> None:
        changed = config != self.llm
        self.llm = config
        s = self.settings
        for key in ("provider", "base_url", "model"):
            s.set_value(f"llm/{key}", getattr(config, key))
        s.set_value("llm/temperature", float(config.temperature))
        s.set_value("llm/timeout", float(config.timeout))
        s.set_value("llm/max_tokens", int(config.max_tokens))
        if changed:
            self.llm_changed.emit()

    def save_hints(self, text: str) -> None:
        self.hints_text = text
        self.settings.set_value("partition_hints", text)

    def save_schema(self, text: str) -> None:
        self.schema_text = text
        self.settings.set_value("schema_hints", text)

    def save_optimizer(self, include_review: bool, disabled_rules: set[str]) -> None:
        changed = (include_review, set(disabled_rules)) != (self.include_review, self.disabled_rules)
        self.include_review = include_review
        self.disabled_rules = set(disabled_rules)
        self.settings.set_value("optimizer/include_review", bool(include_review))
        self.settings.set_value("optimizer/disabled_rules", sorted(self.disabled_rules))
        if changed:
            self.optimizer_changed.emit()

    def save_finding_rules(self, disabled: set[str]) -> None:
        """Rule names (from :mod:`sqlanalyzer.rules_registry`) the static/engine/schema/partition
        analyzers should not report - distinct from the optimizer's own rewrite rules above."""
        changed = set(disabled) != self.disabled_finding_rules
        self.disabled_finding_rules = set(disabled)
        self.settings.set_value("findings/disabled_rules", sorted(self.disabled_finding_rules))
        if changed:
            self.optimizer_changed.emit()  # re-analyze; findings are recomputed alongside the rewrite

    def save_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.settings.set_value("theme", theme.value)
        self.theme_changed.emit(theme)

    def _load(self) -> None:
        s = self.settings
        self.hints_text = str(s.value("partition_hints", "") or "")
        self.schema_text = str(s.value("schema_hints", "") or "")
        self.include_review = bool(s.value("optimizer/include_review", True))
        if s.contains("llm/model") or s.contains("llm/base_url"):
            self.llm = LlmConfig(
                provider=str(s.value("llm/provider", self.llm.provider)),
                base_url=str(s.value("llm/base_url", self.llm.base_url)),
                model=str(s.value("llm/model", self.llm.model)),
                temperature=float(s.value("llm/temperature", self.llm.temperature) or 0.0),
                timeout=float(s.value("llm/timeout", self.llm.timeout) or 120.0),
                max_tokens=int(s.value("llm/max_tokens", self.llm.max_tokens) or 1536),
            )
        if s.contains("optimizer/disabled_rules"):
            raw = s.value("optimizer/disabled_rules", [])
            if isinstance(raw, str):
                raw = [raw] if raw else []
            self.disabled_rules = {str(r) for r in raw}
        if s.contains("findings/disabled_rules"):
            raw = s.value("findings/disabled_rules", [])
            if isinstance(raw, str):
                raw = [raw] if raw else []
            self.disabled_finding_rules = {str(r) for r in raw}
        if s.contains("theme"):
            try:
                self.theme = Theme(str(s.value("theme", Theme.SYSTEM.value)))
            except ValueError:
                self.theme = Theme.SYSTEM
        for engine in Engine:
            prefix = f"connection/{engine.value}/"
            if s.contains(prefix + "host") or s.contains(prefix + "database"):
                extras = s.value(prefix + "extras", {}) or {}
                self.connections[engine] = ConnectionConfig(
                    engine, str(s.value(prefix + "host", "") or ""), int(s.value(prefix + "port", 0) or 0) or None,
                    str(s.value(prefix + "database", "") or ""), str(s.value(prefix + "schema", "") or ""),
                    str(s.value(prefix + "username", "") or ""), "",
                    {str(k): str(v) for k, v in dict(extras).items()})
