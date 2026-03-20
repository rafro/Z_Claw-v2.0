"""
Gemini provider — Tier 3 API fallback.
Fails gracefully if GEMINI_API_KEY is absent.
Used when Ollama is offline and Claude-level quality isn't required.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from providers.base import BaseProvider, ProviderError

log = logging.getLogger(__name__)


class GeminiProvider(BaseProvider):
    """
    Google Gemini via API.
    Requires GEMINI_API_KEY in environment.
    is_available() returns False immediately if key is missing.
    """

    DEFAULT_MODEL = "gemini-2.0-flash"

    def __init__(self, model: str | None = None):
        self._model = model or os.getenv("GEMINI_MODEL", self.DEFAULT_MODEL)

    @property
    def provider_id(self) -> str:
        return "gemini"

    def is_available(self) -> bool:
        return bool(os.getenv("GEMINI_API_KEY", "").strip())

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        if not self.is_available():
            raise ProviderError("GEMINI_API_KEY not set — Gemini unavailable", retryable=False)

        try:
            import google.generativeai as genai
        except ImportError:
            raise ProviderError("google-generativeai package not installed", retryable=False)

        try:
            genai.configure(api_key=os.environ["GEMINI_API_KEY"])
            model = genai.GenerativeModel(self._model)

            # Convert OpenAI-style messages to Gemini format
            system_parts = []
            history = []
            last_user_msg = ""

            for m in messages:
                role = m["role"]
                content = m["content"]
                if role == "system":
                    system_parts.append(content)
                elif role == "user":
                    last_user_msg = content
                    if history:
                        history.append({"role": "user", "parts": [content]})
                elif role == "assistant":
                    history.append({"role": "model", "parts": [content]})

            # Prepend system message to first user turn if present
            if system_parts and not history:
                prompt = "\n\n".join(system_parts) + "\n\n" + last_user_msg
            else:
                prompt = last_user_msg

            gen_config: dict[str, Any] = {
                "temperature": temperature,
                "max_output_tokens": max_tokens,
            }
            if json_mode:
                gen_config["response_mime_type"] = "application/json"

            response = model.generate_content(
                prompt,
                generation_config=genai.GenerationConfig(**gen_config),
            )
            return response.text.strip()

        except Exception as e:
            log.error("GeminiProvider chat failed: %s", e)
            raise ProviderError(str(e), retryable=False) from e
