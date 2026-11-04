"""Ask a local LLM for a rewrite and validate it against the rule engine."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp

from ..models import Finding
from ..optimizer import OptimizationResult, optimize_query
from ..parsing import format_sql, parse_sql
from ..partition import PartitionHints
from .client import LlmClient, LlmConfig, LlmError
from .prompt import SYSTEM, LlmSuggestion, build_prompt, parse_response
from .review import LlmReview, review_llm_sql


@dataclass
class LlmOptimization:
    """Everything about one round trip to the local model."""

    config: LlmConfig
    dialect: str
    original_sql: str
    prompt: str = ""
    raw_response: str = ""
    suggestion: LlmSuggestion | None = None
    review: LlmReview | None = None
    rules: OptimizationResult | None = None  # what the deterministic engine produced, for comparison
    error: str = ""
    seconds: float = 0.0
    statements: list[exp.Expr] = field(default_factory=list)

    @property
    def sql(self) -> str:
        return self.suggestion.sql if self.suggestion else ""

    @property
    def ok(self) -> bool:
        """A usable, validated suggestion (warnings are allowed, failures are not)."""
        return bool(self.sql) and self.review is not None and self.review.ok and not self.error

    @property
    def applicable(self) -> bool:
        return self.ok

    def summary(self) -> str:
        if self.error:
            return f"Local model failed: {self.error}"
        if not self.sql:
            return "The model did not return any SQL."
        head = f"{self.config.display()} answered in {self.seconds:.1f}s"
        if self.review is not None:
            return f"{head}. {self.review.summary()}"
        return head + "."


def llm_optimize(sql: str, dialect: str, client: LlmClient, *, schema: Mapping[str, Any] | None = None,
                 schema_text: str = "", partition_text: str = "", partition_hints: PartitionHints | None = None,
                 findings: list[Finding] | None = None, rules: OptimizationResult | None = None,
                 pretty: bool = True) -> LlmOptimization:
    """Prompt ``client`` for a faster version of ``sql`` and validate the answer.

    ``rules`` is the deterministic rewrite of the same query: it is shown to the
    model as a reference and used afterwards to corroborate the answer. When it
    is not supplied it is computed here.
    """
    result = LlmOptimization(client.config, dialect, sql)
    parsed = parse_sql(sql, dialect)
    if not parsed.ok:
        result.error = "The query does not parse; fix the syntax errors first."
        return result
    if rules is None:
        try:
            rules = optimize_query(parsed.statements, dialect, schema=schema)
        except Exception:  # noqa: BLE001 - the rule engine is optional context here
            rules = None
    result.rules = rules
    result.prompt = build_prompt(sql, dialect, schema_text=schema_text, findings=findings, rules=rules,
                                 partition_text=partition_text)
    start = time.perf_counter()
    try:
        result.raw_response = client.complete(result.prompt, SYSTEM)
    except LlmError as exc:
        result.error = str(exc)
        return result
    except Exception as exc:  # noqa: BLE001 - never let a transport problem escape
        result.error = f"{exc.__class__.__name__}: {exc}"
        return result
    finally:
        result.seconds = time.perf_counter() - start

    result.suggestion = parse_response(result.raw_response)
    if not result.suggestion.ok:
        result.error = result.suggestion.parse_error or "No SQL found in the model's answer."
        return result
    result.review = review_llm_sql(sql, result.suggestion.sql, dialect, schema=schema,
                                   partition_hints=partition_hints, rules=rules,
                                   declared_changes=result.suggestion.changes)
    result.statements = result.review.statements
    if pretty and result.review.statements:
        try:
            result.suggestion.sql = format_sql(result.suggestion.sql, dialect)
        except Exception:  # noqa: BLE001 - keep the raw SQL when formatting fails
            pass
    return result
