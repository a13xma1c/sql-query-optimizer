"""Minimal client for locally hosted LLMs (Ollama or any OpenAI-compatible server).

No extra dependency: requests are plain ``urllib`` calls to ``localhost``. Two
providers cover everything that is commonly run locally:

* ``ollama``  - ``POST {base_url}/api/chat`` (Ollama, default port 11434)
* ``openai``  - ``POST {base_url}/v1/chat/completions`` (llama.cpp server,
  LM Studio, vLLM, text-generation-webui, LocalAI, ...)

The transport is injectable so tests never touch the network.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

OLLAMA = "ollama"
OPENAI = "openai"
PROVIDERS = (OLLAMA, OPENAI)
DEFAULT_URLS = {OLLAMA: "http://localhost:11434", OPENAI: "http://localhost:8080"}

Transport = Callable[[str, dict | None, dict[str, str], float], dict]


class LlmError(RuntimeError):
    """The local model could not be reached or returned something unusable."""


@dataclass(frozen=True)
class LlmConfig:
    provider: str = OLLAMA
    base_url: str = DEFAULT_URLS[OLLAMA]
    model: str = "qwen2.5-coder:7b"
    temperature: float = 0.0
    timeout: float = 120.0
    max_tokens: int = 1536
    api_key: str = ""  # only needed by some OpenAI-compatible servers; never persisted
    extra: dict[str, Any] = field(default_factory=dict)

    def normalized_url(self) -> str:
        return self.base_url.rstrip("/") or DEFAULT_URLS.get(self.provider, "")

    def display(self) -> str:
        return f"{self.model} @ {self.normalized_url()} ({self.provider})"


def http_json(url: str, payload: dict | None, headers: dict[str, str], timeout: float) -> dict:
    """POST ``payload`` as JSON (GET when it is ``None``) and decode the JSON answer."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - local http by design
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # pragma: no cover - server dependent
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise LlmError(f"{url} returned HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise LlmError(f"Cannot reach {url}: {exc.reason}. Is the local model server running?") from exc
    except TimeoutError as exc:
        raise LlmError(f"{url} timed out after {timeout:.0f}s.") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise LlmError(f"{url} did not return JSON: {body[:200]}") from exc
    if not isinstance(parsed, dict):
        raise LlmError(f"{url} returned unexpected JSON: {body[:200]}")
    return parsed


class LlmClient:
    """Chat completion + model listing for a local server."""

    def __init__(self, config: LlmConfig, transport: Transport | None = None) -> None:
        self.config = config
        self._transport = transport or http_json

    # ------------------------------------------------------------------ requests
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {}

    def complete(self, prompt: str, system: str = "") -> str:
        """Send a single chat turn and return the assistant message."""
        cfg = self.config
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        if cfg.provider == OLLAMA:
            payload = {"model": cfg.model, "messages": messages, "stream": False,
                       "options": {"temperature": cfg.temperature, "num_predict": cfg.max_tokens, **cfg.extra}}
            data = self._transport(f"{cfg.normalized_url()}/api/chat", payload, self._headers(), cfg.timeout)
            content = (data.get("message") or {}).get("content")
            if content is None and isinstance(data.get("response"), str):  # /api/generate shaped answer
                content = data["response"]
        elif cfg.provider == OPENAI:
            payload = {"model": cfg.model, "messages": messages, "temperature": cfg.temperature,
                       "max_tokens": cfg.max_tokens, "stream": False, **cfg.extra}
            data = self._transport(f"{cfg.normalized_url()}/v1/chat/completions", payload, self._headers(),
                                   cfg.timeout)
            choices = data.get("choices") or []
            content = (choices[0].get("message") or {}).get("content") if choices else None
        else:
            raise LlmError(f"Unknown provider {cfg.provider!r}; use one of {', '.join(PROVIDERS)}.")
        if not isinstance(content, str) or not content.strip():
            raise LlmError(f"The model returned an empty answer ({json.dumps(data)[:200]}).")
        return content

    def models(self) -> list[str]:
        cfg = self.config
        if cfg.provider == OLLAMA:
            data = self._transport(f"{cfg.normalized_url()}/api/tags", None, self._headers(), cfg.timeout)
            return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))
        data = self._transport(f"{cfg.normalized_url()}/v1/models", None, self._headers(), cfg.timeout)
        return sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))

    def ping(self) -> str:
        """Check the server and whether the configured model is available."""
        models = self.models()
        if not models:
            return f"Connected to {self.config.normalized_url()}, but the server lists no models."
        known = self.config.model in models or any(m.split(":")[0] == self.config.model.split(":")[0] for m in models)
        note = "" if known else f" Warning: '{self.config.model}' is not in the list - pull/load it first."
        return f"Connected to {self.config.normalized_url()}; {len(models)} model(s) available.{note}"
