"""Minimal LLM client interface plus a Gemini REST adapter (no extra SDK dependency)."""
from __future__ import annotations

import logging
import os
import random
import time
from typing import Callable, List, Optional, Protocol

import requests

from backend.config import get_llm_model, get_llm_timeout, int_env, float_env
from backend.utils.errors import sanitize_error

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


logger = logging.getLogger(__name__)

# HTTP statuses worth retrying: rate limit and temporary unavailability. Auth errors (401/403),
# bad requests (400/404) and everything else are permanent and are never retried.
TRANSIENT_STATUS = frozenset({429, 503})
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODELS = {"gemini": "gemini-2.5-flash", "openrouter": "google/gemini-2.5-flash"}


class LLMError(RuntimeError):
    """The LLM could not produce a usable answer (not configured, timeout, HTTP error, blocked, truncated).

    ``transient`` is True only for failures that may succeed if retried (HTTP 429/503, timeouts).
    """

    def __init__(self, message: str, transient: bool = False):
        super().__init__(message)
        self.transient = transient


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


class GeminiClient:
    provider = "gemini"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None, max_output_tokens: int = 1024):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY")
        self.model = model or get_llm_model()
        self.timeout = timeout if timeout is not None else get_llm_timeout()
        self.max_output_tokens = max_output_tokens

    def complete(self, system: str, user: str) -> str:
        if not self.api_key or self.api_key.startswith("YOUR_"):
            raise LLMError("LLM is not configured: set GEMINI_API_KEY in your .env file.")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0,
                "maxOutputTokens": self.max_output_tokens,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        try:
            resp = requests.post(
                GEMINI_URL.format(model=self.model),
                headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
                json=body,
                timeout=(10, self.timeout),
            )
        except requests.Timeout as exc:
            raise LLMError("LLM request timed out.", transient=True) from exc
        except requests.RequestException as exc:
            raise LLMError(f"Could not reach the LLM service: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            raise LLMError("LLM rate limit or quota exceeded (HTTP 429).", transient=True)
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))[:200]
            except ValueError:
                pass
            raise LLMError(f"LLM request failed (HTTP {resp.status_code}). {sanitize_error(RuntimeError(detail))[len('RuntimeError: '):]}".strip(), transient=resp.status_code in TRANSIENT_STATUS)

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("LLM returned a non-JSON response.") from exc
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        if blocked:
            raise LLMError(f"LLM blocked the request ({blocked}).")
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("LLM returned no candidates.")
        cand = cands[0]
        if cand.get("finishReason") not in (None, "STOP"):
            raise LLMError(f"LLM did not finish normally ({cand.get('finishReason')}).")
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])).strip()
        if not text:
            raise LLMError("LLM returned an empty answer.")
        return text


class OpenRouterClient:
    """OpenRouter (OpenAI-compatible chat completions) adapter."""

    provider = "openrouter"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 timeout: Optional[float] = None, max_output_tokens: int = 1024):
        self.api_key = api_key if api_key is not None else os.getenv("OPENROUTER_API_KEY")
        self.model = model or DEFAULT_MODELS["openrouter"]
        self.timeout = timeout if timeout is not None else get_llm_timeout()
        self.max_output_tokens = max_output_tokens

    def complete(self, system: str, user: str) -> str:
        if not self.api_key or self.api_key.startswith("YOUR_"):
            raise LLMError("LLM is not configured: set OPENROUTER_API_KEY in your .env file.")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
        }
        try:
            resp = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=body,
                timeout=(10, self.timeout),
            )
        except requests.Timeout as exc:
            raise LLMError("LLM request timed out.", transient=True) from exc
        except requests.RequestException as exc:
            raise LLMError(f"Could not reach the LLM service: {type(exc).__name__}") from exc

        if resp.status_code != 200:
            detail = ""
            try:
                detail = str((resp.json().get("error") or {}).get("message", ""))[:200]
            except (ValueError, AttributeError):
                pass
            kind = "rate limit or quota exceeded" if resp.status_code == 429 else "request failed"
            msg = f"LLM {kind} (HTTP {resp.status_code}). {sanitize_error(RuntimeError(detail))[len('RuntimeError: '):]}".strip()
            raise LLMError(msg, transient=resp.status_code in TRANSIENT_STATUS)
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError("LLM returned a non-JSON response.") from exc
        choices = data.get("choices") or []
        if not choices:
            err = data.get("error") or {}
            code = err.get("code")
            raise LLMError("LLM returned no choices.", transient=isinstance(code, int) and code in TRANSIENT_STATUS)
        choice = choices[0]
        if choice.get("finish_reason") not in (None, "stop"):
            raise LLMError(f"LLM did not finish normally ({choice.get('finish_reason')}).")
        text = ((choice.get("message") or {}).get("content") or "").strip()
        if not text:
            raise LLMError("LLM returned an empty answer.")
        return text


class _BrokenClient:
    """Stands in for a misconfigured provider so the failure is reported, not raised at startup."""

    def __init__(self, provider: str, model: str, error: LLMError):
        self.provider, self.model, self._error = provider, model, error

    def complete(self, system: str, user: str) -> str:
        raise self._error


def _label(client) -> str:
    return f"{getattr(client, 'provider', 'llm')}/{getattr(client, 'model', '?')}"


class ResilientLLM:
    """Primary client with bounded exponential-backoff retries, then an optional fallback client.

    * Only transient errors (HTTP 429/503, timeouts) are retried, at most ``max_retries`` times.
    * Any primary failure (retries exhausted, invalid or missing key, permanent error) moves on to the fallback.
    * If every client fails, one controlled ``LLMError`` summarising each failure is raised.
    * ``model`` reflects the client that actually answered.
    """

    def __init__(self, clients: List, max_retries: int = 2, base_delay: float = 1.0, max_delay: float = 8.0,
                 total_timeout: float = 120.0, sleep: Optional[Callable[[float], None]] = None,
                 monotonic: Callable[[], float] = time.monotonic, rng: Callable[[], float] = random.random):
        if not clients:
            raise ValueError("ResilientLLM needs at least one client")
        self.clients = clients
        self.model = clients[0].model
        self.max_retries, self.base_delay, self.max_delay = max(0, max_retries), base_delay, max_delay
        self.total_timeout = total_timeout
        self._sleep, self._monotonic, self._rng = sleep, monotonic, rng

    def _call_with_retries(self, client, system: str, user: str, started: float) -> str:
        attempt = 0
        while True:
            try:
                return client.complete(system, user)
            except LLMError as exc:
                if not exc.transient or attempt >= self.max_retries:
                    raise
                delay = min(self.max_delay, self.base_delay * (2 ** attempt))
                delay *= 1 + 0.2 * (self._rng() * 2 - 1)  # +/-20% jitter
                if self._monotonic() - started + delay > self.total_timeout:
                    raise  # time budget exhausted: stop retrying
                logger.warning("LLM %s transient failure, retry %d/%d in %.1fs", _label(client),
                               attempt + 1, self.max_retries, delay)
                (self._sleep or time.sleep)(delay)
                attempt += 1

    def complete(self, system: str, user: str) -> str:
        started = self._monotonic()
        failures = []
        for i, client in enumerate(self.clients):
            try:
                text = self._call_with_retries(client, system, user, started)
                self.model = client.model
                if i:
                    logger.warning("LLM answered by fallback %s", _label(client))
                return text
            except LLMError as exc:
                if len(self.clients) == 1:
                    raise
                failures.append(f"{_label(client)}: {exc}")
                logger.warning("LLM %s failed (%s)", _label(client), "transient" if exc.transient else "permanent")
        raise LLMError("All configured LLM providers failed. " + " | ".join(failures))


def _build_client(provider: str, model: Optional[str]):
    provider = (provider or "").strip().lower()
    if provider == "gemini":
        return GeminiClient(model=model or DEFAULT_MODELS["gemini"])
    if provider == "openrouter":
        return OpenRouterClient(model=model or DEFAULT_MODELS["openrouter"])
    return _BrokenClient(provider or "?", model or "?", LLMError(
        f"Unknown LLM provider '{provider}'. Supported: gemini, openrouter."))


def build_llm_from_env() -> ResilientLLM:
    """LLM_PROVIDER/LLM_MODEL = primary (default gemini); LLM_FALLBACK_PROVIDER/LLM_FALLBACK_MODEL = optional fallback."""
    primary = _build_client(os.getenv("LLM_PROVIDER") or "gemini", os.getenv("LLM_MODEL") or None)
    clients = [primary]
    fb_provider = (os.getenv("LLM_FALLBACK_PROVIDER") or "").strip()
    if fb_provider:
        clients.append(_build_client(fb_provider, os.getenv("LLM_FALLBACK_MODEL") or None))
    return ResilientLLM(
        clients,
        max_retries=int_env("LLM_MAX_RETRIES", 2),
        base_delay=float_env("LLM_RETRY_BASE_SECONDS", 1.0),
        max_delay=float_env("LLM_RETRY_MAX_SECONDS", 8.0),
        total_timeout=float_env("LLM_TOTAL_TIMEOUT_SECONDS", 120.0),
    )


def get_llm_client() -> LLMClient:
    """FastAPI dependency (overridden in tests). Constructing it never fails; calling it may."""
    return build_llm_from_env()
