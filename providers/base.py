"""
Abstract base class for all OpenClaw providers.
Every provider must implement: chat(), is_available(), provider_id.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ProviderError(Exception):
    """Raised when a provider call fails."""
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


class BaseProvider(ABC):
    """
    Abstract LLM/inference provider.

    All providers expose a uniform interface so orchestrators and workers
    never need to know which backend is running.
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Unique string identifier, e.g. 'ollama:qwen2.5:7b' or 'claude'."""

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the provider can accept requests right now."""

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        """
        Send a chat request and return the response text.

        Args:
            messages: List of {"role": "...", "content": "..."} dicts.
            temperature: Sampling temperature.
            max_tokens: Max tokens to generate.
            json_mode: If True, hint to model to return valid JSON.

        Returns:
            Response text as string.

        Raises:
            ProviderError: On failure. Check .retryable to decide whether to move on.
        """

    def chat_json(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.05,
        max_tokens: int = 2048,
    ) -> Any:
        """
        Convenience wrapper: call chat() with json_mode=True and parse the response.
        Raises ValueError if the response isn't parseable JSON.
        """
        import json
        import re

        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group())
            raise ValueError(f"Provider {self.provider_id} did not return valid JSON: {text[:200]}")

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.provider_id}>"
