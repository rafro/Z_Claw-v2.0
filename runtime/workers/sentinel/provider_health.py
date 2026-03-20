"""
ProviderHealthWorker — checks all configured providers.
Pure deterministic — no LLM needed.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)


class ProviderHealthWorker:

    def run(self) -> dict[str, Any]:
        """
        Check health of all providers.

        Returns:
        {
            "providers": {
                "ollama:model": {
                    "status": "available" | "unavailable",
                    "latency_ms": int | None,
                    "last_checked": str,
                },
                ...
            },
            "summary": str,
            "healthy_count": int,
            "total_count": int,
        }
        """
        from datetime import datetime, timezone
        from runtime.config import (
            OLLAMA_HOST, MODEL_7B, MODEL_8B, MODEL_CODER_7B,
            MODEL_CODER_14B, MODEL_14B_HOST,
        )
        from runtime.ollama_client import is_available as ollama_is_available

        results: dict[str, Any] = {}

        # ── Ollama models ─────────────────────────────────────────────────
        ollama_models = [
            (MODEL_7B, OLLAMA_HOST),
            (MODEL_8B, OLLAMA_HOST),
            (MODEL_CODER_7B, OLLAMA_HOST),
            (MODEL_CODER_14B, MODEL_14B_HOST),
        ]
        for model, host in ollama_models:
            key = f"ollama:{model}"
            t0 = time.monotonic()
            try:
                available = ollama_is_available(model, host)
                latency_ms = int((time.monotonic() - t0) * 1000)
                results[key] = {
                    "status": "available" if available else "unavailable",
                    "latency_ms": latency_ms if available else None,
                    "host": host,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                }
            except Exception as e:
                results[key] = {
                    "status": "error",
                    "error": str(e),
                    "host": host,
                    "last_checked": datetime.now(timezone.utc).isoformat(),
                }

        # ── Groq (primary external — key check only) ─────────────────────
        results["groq"] = {
            "status": "available" if os.getenv("GROQ_API_KEY", "").strip() else "unavailable",
            "note": "API key presence only — not pinged",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

        # ── DeepSeek (backup external — key check only) ───────────────────
        results["deepseek"] = {
            "status": "available" if os.getenv("DEEPSEEK_API_KEY", "").strip() else "unavailable",
            "note": "API key presence only — not pinged",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

        # ── Gemini (last-resort fallback — key check only) ────────────────
        results["gemini"] = {
            "status": "available" if os.getenv("GEMINI_API_KEY", "").strip() else "unavailable",
            "note": "API key presence only — not pinged",
            "last_checked": datetime.now(timezone.utc).isoformat(),
        }

        healthy = sum(1 for v in results.values() if v.get("status") == "available")
        total = len(results)

        summary_parts = [f"{k}: {v['status']}" for k, v in results.items()]
        summary = f"{healthy}/{total} providers healthy. " + " | ".join(summary_parts[:4])

        log.info("ProviderHealth: %d/%d healthy", healthy, total)

        return {
            "providers": results,
            "summary": summary,
            "healthy_count": healthy,
            "total_count": total,
        }
