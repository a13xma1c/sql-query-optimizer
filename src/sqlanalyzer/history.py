"""Query history: what was analyzed, when, with which outcome (a JSON file, no GUI dependency)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ENV_VAR = "SQLANALYZER_HISTORY_FILE"
DEFAULT_LIMIT = 500


def default_path() -> Path:
    override = os.environ.get(ENV_VAR)
    if override:
        return Path(override)
    from .gui.settings import default_path as settings_path

    return settings_path().with_name("history.json")


def normalize(sql: str) -> str:
    """Whitespace/case-insensitive key so re-running the same query updates one entry."""
    return re.sub(r"\s+", " ", sql.strip()).lower()


def fingerprint(sql: str, engine: str) -> str:
    return hashlib.sha1(f"{engine}\n{normalize(sql)}".encode()).hexdigest()[:16]


@dataclass
class HistoryEntry:
    id: str
    engine: str
    sql: str
    first_seen: str  # ISO timestamps (UTC)
    last_seen: str
    runs: int = 1
    source: str = "analyze"  # analyze | explain | rewrite | llm | load
    findings: int = 0
    errors: int = 0
    warnings: int = 0
    complexity: int | None = None
    cost: float | None = None  # offline cost estimate
    rewrites: int = 0
    plan_source: str = ""  # "" | "explain" | "analyze" | "pasted"
    tags: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        line = next((ln.strip() for ln in self.sql.splitlines() if ln.strip() and not ln.strip().startswith("--")),
                    self.sql.strip())
        return line[:90] + ("…" if len(line) > 90 else "")

    @property
    def last_seen_local(self) -> str:
        try:
            return datetime.fromisoformat(self.last_seen).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:  # pragma: no cover - corrupt file
            return self.last_seen


class QueryHistory:
    def __init__(self, path: Path | str | None = None, limit: int = DEFAULT_LIMIT) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.limit = limit
        self._entries: dict[str, HistoryEntry] = {}
        self.load()

    # ------------------------------------------------------------------ access
    def entries(self, *, engine: str | None = None, query: str = "") -> list[HistoryEntry]:
        """Newest first, optionally filtered by engine and a case-insensitive text search."""
        needle = query.strip().lower()
        items = [e for e in self._entries.values()
                 if (engine is None or e.engine == engine)
                 and (not needle or needle in e.sql.lower() or needle in " ".join(e.tags).lower())]
        return sorted(items, key=lambda e: e.last_seen, reverse=True)

    def get(self, entry_id: str) -> HistoryEntry | None:
        return self._entries.get(entry_id)

    def __len__(self) -> int:
        return len(self._entries)

    # ------------------------------------------------------------------ update
    def record(self, sql: str, engine: str, *, source: str = "analyze", findings: int = 0, errors: int = 0,
               warnings: int = 0, complexity: int | None = None, cost: float | None = None, rewrites: int = 0,
               plan_source: str = "", tags: list[str] | None = None) -> HistoryEntry | None:
        """Add or refresh the entry for ``sql``; returns ``None`` for empty SQL."""
        if not sql.strip():
            return None
        now = datetime.now(UTC).isoformat(timespec="microseconds")
        key = fingerprint(sql, engine)
        entry = self._entries.get(key)
        if entry is None:
            entry = HistoryEntry(key, engine, sql, now, now)
            self._entries[key] = entry
        else:
            entry.runs += 1
            entry.last_seen = now
            entry.sql = sql  # keep the latest formatting
        entry.source = source
        entry.findings, entry.errors, entry.warnings = findings, errors, warnings
        entry.complexity, entry.cost, entry.rewrites = complexity, cost, rewrites
        entry.plan_source = plan_source or entry.plan_source
        if tags:
            entry.tags = sorted(set(entry.tags) | set(tags))
        self._trim()
        self.save()
        return entry

    def tag(self, entry_id: str, tags: list[str]) -> None:
        entry = self._entries.get(entry_id)
        if entry is not None:
            entry.tags = sorted(set(tags))
            self.save()

    def remove(self, entry_id: str) -> bool:
        if entry_id in self._entries:
            del self._entries[entry_id]
            self.save()
            return True
        return False

    def clear(self) -> None:
        self._entries.clear()
        self.save()

    def _trim(self) -> None:
        if len(self._entries) > self.limit:
            for old in self.entries()[self.limit:]:
                del self._entries[old.id]

    # -------------------------------------------------------------------- file
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = []
        self._entries = {}
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                entry = HistoryEntry(**{k: v for k, v in item.items() if k in HistoryEntry.__dataclass_fields__})
            except TypeError:
                continue
            if entry.id:
                self._entries[entry.id] = entry

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".history-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump([asdict(e) for e in self.entries()], fh, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def export_sql(self) -> str:
        """All queries as one SQL script with a header comment per entry."""
        parts = []
        for e in self.entries():
            header = f"-- {e.last_seen_local} · {e.engine} · {e.findings} finding(s)"
            if e.tags:
                header += " · " + ", ".join(e.tags)
            parts.append(f"{header}\n{e.sql.strip().rstrip(';')};\n")
        return "\n".join(parts)


__all__ = ["DEFAULT_LIMIT", "ENV_VAR", "HistoryEntry", "QueryHistory", "default_path", "fingerprint", "normalize"]

