"""Shared result types used by every analyzer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"

    @property
    def rank(self) -> int:
        return {"error": 0, "warning": 1, "info": 2}[self.value]


class Category(StrEnum):
    SYNTAX = "syntax"
    LINT = "lint"
    JOIN = "join"
    SCAN = "scan"
    PARTITION = "partition"
    PLAN = "plan"
    COMPLEXITY = "complexity"
    ENGINE = "engine"  # rules specific to one engine family
    SCHEMA = "schema"  # checks against the declared/imported schema (types, keys, statistics)


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: Severity
    category: Category
    message: str
    suggestion: str = ""
    line: int | None = None  # 1-based, in the analyzed SQL text
    col: int | None = None
    node_id: int | None = None  # plan node id for plan-level findings

    @property
    def key(self) -> tuple[str, str]:
        """Identity used when diffing findings between two query versions."""
        return (self.rule, re.sub(r" #\d+\]", "]", self.message))


def sort_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (f.severity.rank, f.line or 0, f.col or 0, f.rule))
