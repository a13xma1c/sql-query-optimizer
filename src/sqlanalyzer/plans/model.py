"""Engine-neutral execution plan model."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from ..dialects import Engine

# Normalized operator kinds
SCAN = "scan"  # full / sequential scan
INDEX_SCAN = "index_scan"
JOIN = "join"
AGG = "aggregate"
SORT = "sort"
EXCHANGE = "exchange"  # gather / repartition / remote source
APPEND = "append"
OTHER = "other"


@dataclass(eq=False)
class PlanNode:
    id: int
    op: str  # display name as reported by the engine
    kind: str = OTHER
    relation: str | None = None  # table scanned (scans only)
    alias: str | None = None
    join_type: str | None = None  # INNER / LEFT / ...
    join_algo: str | None = None  # hash / nested_loop / merge
    est_rows: float | None = None  # per execution (loop)
    actual_rows: float | None = None  # total over all loops
    cost: float | None = None  # inclusive
    time_ms: float | None = None  # inclusive
    self_time_ms: float | None = None  # exclusive
    filter: str | None = None
    condition: str | None = None  # join / index condition
    attrs: dict[str, Any] = field(default_factory=dict)
    children: list[PlanNode] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    def walk(self) -> Iterator[PlanNode]:
        yield self
        for c in self.children:
            yield from c.walk()

    @property
    def loops(self) -> float:
        return float(self.attrs.get("loops") or 1)

    @property
    def est_total_rows(self) -> float | None:
        if self.est_rows is None:
            return None
        return self.est_rows * (self.loops if self.actual_rows is not None else 1)

    @property
    def output_rows(self) -> float | None:
        return self.actual_rows if self.actual_rows is not None else self.est_rows

    @property
    def self_cost(self) -> float | None:
        if self.cost is None:
            return None
        return max(self.cost - sum(c.cost or 0.0 for c in self.children), 0.0)

    @property
    def title(self) -> str:
        return f"{self.op} on {self.relation}" if self.relation else self.op

    @property
    def signature(self) -> tuple[str, str]:
        """Identity used when aligning two plans."""
        return (self.op.lower(), (self.relation or "").lower())


@dataclass
class Plan:
    engine: Engine
    root: PlanNode
    raw: str = ""
    analyzed: bool = False
    stats: dict[str, Any] = field(default_factory=dict)  # engine-global stats

    def __post_init__(self) -> None:
        for i, node in enumerate(self.root.walk()):  # stable pre-order ids
            node.id = i

    def nodes(self) -> list[PlanNode]:
        return list(self.root.walk())

    def by_id(self) -> dict[int, PlanNode]:
        return {n.id: n for n in self.root.walk()}

    @property
    def total_cost(self) -> float | None:
        return self.root.cost

    @property
    def total_time_ms(self) -> float | None:
        if self.root.time_ms is not None:
            return self.root.time_ms
        times = [n.self_time_ms for n in self.root.walk() if n.self_time_ms is not None]
        return sum(times) if times else None


class IdCounter:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        n = self._n
        self._n += 1
        return n


class PlanParseError(ValueError):
    """The text is not a plan in the expected engine format."""


def to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None
