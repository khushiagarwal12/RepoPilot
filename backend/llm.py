"""Minimal LLM client interface plus a Gemini REST adapter (no extra SDK dependency)."""
from __future__ import annotations

import os
from typing import Optional, Protocol

import requests

from backend.config import get_llm_model, get_llm_timeout
from backend.utils.errors import sanitize_error

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMError(RuntimeError):
    """The LLM could not produce a usable answer (not configured, timeout, HTTP error, blocked, truncated)."""


class LLMClient(Protocol):
    model: str

    def complete(self, system: str, user: str) -> str: ...


class GeminiClient:
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
            raise LLMError("LLM request timed out.") from exc
        except requests.RequestException as exc:
            raise LLMError(f"Could not reach the LLM service: {type(exc).__name__}") from exc

        if resp.status_code == 429:
            raise LLMError("LLM rate limit or quota exceeded (HTTP 429).")
        if resp.status_code != 200:
            detail = ""
            try:
                detail = str(resp.json().get("error", {}).get("message", ""))[:200]
            except ValueError:
                pass
            raise LLMError(f"LLM request failed (HTTP {resp.status_code}). {sanitize_error(RuntimeError(detail))[len('RuntimeError: '):]}".strip())

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


def get_llm_client() -> LLMClient:
    """FastAPI dependency (overridden in tests). Constructing it never fails; calling it may."""
    return GeminiClient()
