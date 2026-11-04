"""Optional rewrite suggestions from a locally hosted LLM, validated against the rule set.

Everything in this package is offline by design: requests go to a model server on
``localhost`` (Ollama or any OpenAI-compatible endpoint) and the answer is only
accepted after :mod:`sqlanalyzer.llm.review` has checked it against the original
query and the deterministic rule engine.
"""

from __future__ import annotations

from .client import DEFAULT_URLS, OLLAMA, OPENAI, PROVIDERS, LlmClient, LlmConfig, LlmError
from .optimize import LlmOptimization, llm_optimize
from .prompt import SYSTEM, LlmSuggestion, build_prompt, parse_response
from .review import FAIL, PASS, WARN, Check, LlmReview, review_llm_sql

__all__ = [
    "DEFAULT_URLS",
    "OLLAMA",
    "OPENAI",
    "PROVIDERS",
    "SYSTEM",
    "Check",
    "FAIL",
    "PASS",
    "WARN",
    "LlmClient",
    "LlmConfig",
    "LlmError",
    "LlmOptimization",
    "LlmReview",
    "LlmSuggestion",
    "build_prompt",
    "llm_optimize",
    "parse_response",
    "review_llm_sql",
]
