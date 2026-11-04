"""Bundled SQL samples: one file per anti-pattern (or clean pattern), with expectations in the header.

Each ``samples/*.sql`` file starts with ``-- key: value`` lines::

    -- name: Comma join with a non-sargable date filter
    -- engines: postgres, duckdb            (default: every engine)
    -- expect: implicit-join, non-sargable-predicate   (static finding rules that must fire)
    -- rewrites: explicit-join, sargable-date          (optimizer rules that must apply)
    -- expect-none: true                    (no warning/error finding at all)
    -- schema: yes                          (needs the demo schema hints to fire)
    -- demo: no                             (not executable on the DuckDB demo database)
    -- description: free text

The files are used by the GUI (*Open sample…*) and by the test suite, which runs
every sample through the analyzer on every listed engine and, where possible,
executes the original and the rewritten query on the demo database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from .dialects import Engine

_SAMPLES_DIR = "samples"


@dataclass(frozen=True)
class Sample:
    key: str  # file stem, e.g. ``01_comma_join_nonsargable``
    name: str
    sql: str
    engines: tuple[Engine, ...]
    expect: tuple[str, ...] = ()
    rewrites: tuple[str, ...] = ()
    expect_none: bool = False
    needs_schema: bool = False
    demo: bool = True
    description: str = ""
    path: str = ""
    headers: dict[str, str] = field(default_factory=dict, hash=False, compare=False)

    def supports(self, engine: Engine) -> bool:
        return engine in self.engines


def parse_sample(text: str, key: str = "", path: str = "") -> Sample:
    headers: dict[str, str] = {}
    body: list[str] = []
    in_header = True
    for line in text.splitlines():
        stripped = line.strip()
        if in_header and stripped.startswith("--") and ":" in stripped:
            k, v = stripped[2:].split(":", 1)
            headers[k.strip().lower()] = v.strip()
            continue
        if in_header and not stripped:
            continue
        in_header = False
        body.append(line)
    engines = tuple(Engine(e.strip()) for e in headers.get("engines", "").split(",") if e.strip()) or tuple(Engine)
    return Sample(
        key=key, name=headers.get("name", key), sql="\n".join(body).strip() + "\n", engines=engines,
        expect=_csv(headers.get("expect", "")), rewrites=_csv(headers.get("rewrites", "")),
        expect_none=headers.get("expect-none", "").lower() in ("true", "yes", "1"),
        needs_schema=headers.get("schema", "").lower() in ("true", "yes", "1"),
        demo=headers.get("demo", "yes").lower() not in ("false", "no", "0"),
        description=headers.get("description", ""), path=path, headers=headers)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def sample_files() -> list[Path]:
    root = resources.files(__package__) / _SAMPLES_DIR
    return sorted(Path(str(p)) for p in root.iterdir() if p.name.endswith(".sql"))


def load_samples() -> list[Sample]:
    out = []
    for path in sample_files():
        out.append(parse_sample(path.read_text(encoding="utf-8"), path.stem, str(path)))
    return out


def get_sample(key: str) -> Sample:
    for sample in load_samples():
        if sample.key == key or sample.name == key:
            return sample
    raise KeyError(key)


__all__ = ["Sample", "get_sample", "load_samples", "parse_sample", "sample_files"]
