"""
Ollama inference wrapper.
Handles structured JSON output, timeouts, and availability checks.
"""

import json
import logging
from typing import Any, Optional

import ollama
from ollama import Client, ResponseError

from runtime.config import OLLAMA_HOST, MODEL_14B_HOST

log = logging.getLogger(__name__)

_client_cache: dict[str, Client] = {}


def _client(host: str = OLLAMA_HOST) -> Client:
    if host not in _client_cache:
        _client_cache[host] = Client(host=host)
    return _client_cache[host]


def is_available(model: str, host: str = OLLAMA_HOST) -> bool:
    """Check if a model is loaded and available on the given host."""
    try:
        models = _client(host).list()
        names = [m.model for m in models.models]
        return any(model in n for n in names)
    except Exception as e:
        log.warning("Ollama availability check failed (%s): %s", host, e)
        return False


def chat(
    model: str,
    messages: list[dict],
    host: str = OLLAMA_HOST,
    temperature: float = 0.1,
    max_tokens: int = 2048,
    adapter: str | None = None,
) -> str:
    """Run a chat completion. Returns the response text."""
    kwargs: dict = dict(
        model=model,
        messages=messages,
        options={"temperature": temperature, "num_predict": max_tokens},
    )
    if adapter:
        kwargs["adapter"] = adapter
    resp = _client(host).chat(**kwargs)
    return resp.message.content.strip()


def chat_json(
    model: str,
    messages: list[dict],
    host: str = OLLAMA_HOST,
    temperature: float = 0.05,
    max_tokens: int = 2048,
    adapter: str | None = None,
) -> Any:
    """
    Run a chat completion expecting JSON output.
    Returns parsed dict/list. Raises ValueError if response is not valid JSON.
    """
    kwargs: dict = dict(
        model=model,
        messages=messages,
        format="json",
        options={"temperature": temperature, "num_predict": max_tokens},
    )
    if adapter:
        kwargs["adapter"] = adapter
    resp = _client(host).chat(**kwargs)
    text = resp.message.content.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        # Try to extract JSON from response if model added surrounding text
        import re
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Model did not return valid JSON: {text[:200]}") from e


def pull_if_missing(model: str, host: str = OLLAMA_HOST) -> bool:
    """Pull a model if not already available. Returns True if ready."""
    if is_available(model, host):
        return True
    log.info("Pulling model %s from %s ...", model, host)
    try:
        _client(host).pull(model)
        log.info("Model %s pulled successfully", model)
        return True
    except Exception as e:
        log.error("Failed to pull model %s: %s", model, e)
        return False
