"""
Ollama provider — wraps runtime/ollama_client.py.
Supports model selection and host selection (local vs. remote GPU).
"""

from __future__ import annotations

import logging
from typing import Any

from providers.base import BaseProvider, ProviderError

log = logging.getLogger(__name__)


class OllamaProvider(BaseProvider):
    """
    Local Ollama inference provider.

    Wraps the existing ollama_client without modifying it.
    provider_id format: "ollama:<model_name>"
    """

    def __init__(self, model: str, host: str | None = None):
        from runtime.config import OLLAMA_HOST
        self._model = model
        self._host = host or OLLAMA_HOST

    @property
    def provider_id(self) -> str:
        return f"ollama:{self._model}"

    @property
    def adapter_path(self) -> str | None:
        """Return the LoRA adapter path if one has been set by the router."""
        return getattr(self, "_adapter_path", None)

    def is_available(self) -> bool:
        from runtime.ollama_client import is_available
        return is_available(self._model, self._host)

    def _adapter_kwargs(self) -> dict[str, Any]:
        """Build extra kwargs for Ollama client calls when a LoRA adapter is active."""
        if self.adapter_path:
            return {"adapter": self.adapter_path}
        return {}

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        from runtime.ollama_client import chat, chat_json
        adapter_kw = self._adapter_kwargs()
        try:
            if json_mode:
                result = chat_json(self._model, messages, host=self._host,
                                   temperature=temperature, max_tokens=max_tokens,
                                   **adapter_kw)
                import json
                return json.dumps(result) if not isinstance(result, str) else result
            return chat(self._model, messages, host=self._host,
                        temperature=temperature, max_tokens=max_tokens,
                        **adapter_kw)
        except Exception as e:
            log.error("OllamaProvider(%s) chat failed: %s", self._model, e)
            raise ProviderError(str(e), retryable=False) from e

    def chat_json(self, messages, temperature=0.05, max_tokens=2048) -> Any:
        """Use Ollama's native JSON mode for better reliability."""
        from runtime.ollama_client import chat_json
        adapter_kw = self._adapter_kwargs()
        try:
            return chat_json(self._model, messages, host=self._host,
                             temperature=temperature, max_tokens=max_tokens,
                             **adapter_kw)
        except Exception as e:
            log.error("OllamaProvider(%s) chat_json failed: %s", self._model, e)
            raise ProviderError(str(e), retryable=False) from e
