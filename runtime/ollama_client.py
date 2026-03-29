"""
Ollama inference wrapper.
Handles structured JSON output, timeouts, and availability checks.
Training-data capture is bolted on here so the 19 skills that call
chat() / chat_json() automatically generate JSONL for fine-tuning.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

import ollama
from ollama import Client, ResponseError

from runtime.config import OLLAMA_HOST, MODEL_14B_HOST

log = logging.getLogger(__name__)

_client_cache: dict[str, Client] = {}

CAPTURE_FILE = Path(__file__).resolve().parent.parent / "state" / "training-capture.jsonl"


def _client(host: str = OLLAMA_HOST) -> Client:
    if host not in _client_cache:
        _client_cache[host] = Client(host=host)
    return _client_cache[host]


# ---------------------------------------------------------------------------
# Training-data capture helper
# ---------------------------------------------------------------------------

def _maybe_capture(
    messages: list[dict],
    response: str | None,
    task_type: str,
    json_mode: bool,
    latency_ms: int,
    model: str,
    host: str,
) -> None:
    """Append a JSONL line to state/training-capture.jsonl.

    Matches the exact schema produced by ``CaptureProvider`` so the same
    review / export / format scripts work unchanged.

    This function **never** raises — capture failures are silently ignored
    so that LLM calls are never blocked.
    """
    try:
        # --- filter identical to CaptureProvider._write_capture ---
        if not response or len(response) < 30:
            return

        from datetime import datetime, timezone

        provider_id = f"ollama:{model}"
        ts = datetime.now(timezone.utc).isoformat()

        entry = {
            "ts": ts,
            "task_type": task_type,
            "provider_id": provider_id,
            "messages": messages,
            "response": response,
            "latency_ms": latency_ms,
            "json_mode": json_mode,
        }

        CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CAPTURE_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # --- optional manifest / domain bookkeeping ---
        try:
            from runtime.domain_map import compute_capture_hash, get_domain
            from runtime.training_manifest import record_capture

            entry_hash = compute_capture_hash(messages, response)
            domain = get_domain(task_type)
            record_capture(entry_hash, domain, ts)
        except Exception:
            pass  # modules may not exist yet — that's fine
    except Exception:
        pass  # capture must NEVER block LLM calls


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
    task_type: str = "unknown",
) -> str:
    """Run a chat completion. Returns the response text."""
    start = time.monotonic()
    resp = _client(host).chat(
        model=model,
        messages=messages,
        options={"temperature": temperature, "num_predict": max_tokens},
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    response = resp.message.content.strip()
    _maybe_capture(messages, response, task_type, json_mode=False, latency_ms=latency_ms, model=model, host=host)
    return response


def chat_json(
    model: str,
    messages: list[dict],
    host: str = OLLAMA_HOST,
    temperature: float = 0.05,
    max_tokens: int = 2048,
    task_type: str = "unknown",
) -> Any:
    """
    Run a chat completion expecting JSON output.
    Returns parsed dict/list. Raises ValueError if response is not valid JSON.
    """
    start = time.monotonic()
    resp = _client(host).chat(
        model=model,
        messages=messages,
        format="json",
        options={"temperature": temperature, "num_predict": max_tokens},
    )
    latency_ms = int((time.monotonic() - start) * 1000)
    text = resp.message.content.strip()
    _maybe_capture(messages, text, task_type, json_mode=True, latency_ms=latency_ms, model=model, host=host)
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
