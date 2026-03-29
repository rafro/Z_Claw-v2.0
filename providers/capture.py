from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from providers.base import BaseProvider
from runtime.tools.domain_map import get_domain, compute_capture_hash
from runtime.tools.training_manifest import record_capture

log = logging.getLogger(__name__)

CAPTURE_FILE = Path(__file__).resolve().parent.parent / "state" / "training-capture.jsonl"


class CaptureProvider(BaseProvider):
    def __init__(self, inner: BaseProvider, task_type: str) -> None:
        self._inner = inner
        self._task_type = task_type

    @property
    def provider_id(self) -> str:
        return self._inner.provider_id

    def is_available(self) -> bool:
        return self._inner.is_available()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        start = time.monotonic()
        response = self._inner.chat(messages, temperature, max_tokens, json_mode)
        latency_ms = int((time.monotonic() - start) * 1000)
        self._write_capture(messages, response, json_mode=json_mode, latency_ms=latency_ms)
        return response

    def chat_json(
        self,
        messages: list[dict],
        temperature: float = 0.05,
        max_tokens: int = 2048,
    ) -> Any:
        start = time.monotonic()
        result = self._inner.chat_json(messages, temperature, max_tokens)
        latency_ms = int((time.monotonic() - start) * 1000)
        self._write_capture(messages, json.dumps(result), json_mode=True, latency_ms=latency_ms)
        return result

    def _write_capture(
        self,
        messages: list[dict],
        response: str,
        json_mode: bool,
        latency_ms: int,
    ) -> None:
        if not response or len(response) < 30:
            return
        if self._inner.provider_id == "deterministic":
            return
        try:
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "task_type": self._task_type,
                "provider_id": self._inner.provider_id,
                "messages": messages,
                "response": response,
                "latency_ms": latency_ms,
                "json_mode": json_mode,
            }
            CAPTURE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CAPTURE_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            log.warning("CaptureProvider failed to write capture entry", exc_info=True)

        # Record in training manifest for lineage tracking
        try:
            entry_hash = compute_capture_hash(messages, response)
            domain = get_domain(self._task_type)
            record_capture(entry_hash, domain, datetime.now(timezone.utc).isoformat())
        except Exception:
            pass  # manifest failures must never block LLM calls
