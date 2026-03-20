"""
ProviderRouter — maps task_type → ordered provider chain.
Walks the chain until a provider reports is_available().

This is the single source of truth for model routing.
runtime/config.py SKILL_MODELS is deprecated; import from here instead.
"""

from __future__ import annotations

import logging
from typing import Optional

from providers.base import BaseProvider

log = logging.getLogger(__name__)

# ── Routing table ─────────────────────────────────────────────────────────────
# Each entry: task_type → [primary_provider_key, fallback1, fallback2, ...]
# Provider keys: "ollama:<model>" | "gemini" | "claude" | "deterministic"
#
# GPU notes (updated):
#   Tier 1 (your 9070 XT, ROCm, local):  7B / 8B / coder-7B  — most tasks
#   Tier 2 (your 9070 XT, heavy tasks):  coder-14B           — debug, doc
#   Tier 3 (API fallback):               gemini              — when Ollama down
#   Tier 4 (API, optional premium):      claude              — escalation only
#   Tier 0 (no LLM):                     deterministic       — pure Python

ROUTING_TABLE: dict[str, list[str]] = {
    # ── Opportunity ──────────────────────────────────────────────────────────
    "hard-filter":           ["ollama:qwen2.5:7b-instruct-q4_K_M",          "deterministic"],
    "job-intake":            ["deterministic"],                              # pure HTTP fetch
    "funding-finder":        ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],

    # ── Trading ──────────────────────────────────────────────────────────────
    "market-scan":           ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
    "trading-report":        ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],

    # ── Personal (health-logger is LOCAL ONLY — never cloud fallback) ────────
    "health-logger":         ["ollama:llama3.2:3b"],
    "perf-correlation":      ["ollama:qwen2.5:7b-instruct-q4_K_M"],
    "burnout-monitor":       ["ollama:qwen2.5:7b-instruct-q4_K_M"],

    # ── Dev Automation ───────────────────────────────────────────────────────
    "repo-monitor":          ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],
    "debug-agent":           ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "gemini", "claude"],
    "refactor-scan":         ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],
    "doc-update":            ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "gemini"],
    "opsec-scan":            ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],

    # ── Dev Pipeline ─────────────────────────────────────────────────────────
    "dev-generate":          ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],
    "dev-review":            ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "gemini"],
    "dev-test":              ["deterministic", "ollama:qwen2.5-coder:7b-instruct-q4_K_M"],
    "dev-summarize":         ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
    "dev-finalize":          ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "claude"],

    # ── Architecture / Escalation (Claude optional) ─────────────────────────
    "architecture-review":   ["claude", "gemini"],
    "escalation-reason":     ["claude", "gemini"],

    # ── OP-Sec ───────────────────────────────────────────────────────────────
    "device-posture":        ["deterministic"],
    "breach-check":          ["deterministic"],
    "threat-surface":        ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],
    "cred-audit":            ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],
    "privacy-scan":          ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
    "security-scan":         ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "gemini"],

    # ── Sentinel (health monitoring) ─────────────────────────────────────────
    "sentinel-health":       ["deterministic"],
    "queue-monitor":         ["deterministic"],
    "agent-health":          ["deterministic"],

    # ── Operator Chat ────────────────────────────────────────────────────────
    "chat-operator":         ["ollama:qwen2.5:7b-instruct-q4_K_M",          "claude"],

    # ── Digest synthesis ─────────────────────────────────────────────────────
    "dev-digest":            ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
    "personal-digest":       ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
    "opsec-digest":          ["ollama:qwen2.5:7b-instruct-q4_K_M",          "gemini"],
}


def _build_provider(key: str) -> BaseProvider:
    """Instantiate a provider from its routing-table key."""
    if key == "deterministic":
        from providers.deterministic_provider import DeterministicProvider
        return DeterministicProvider()

    if key == "claude":
        from providers.anthropic_provider import AnthropicProvider
        return AnthropicProvider()

    if key == "gemini":
        from providers.gemini_provider import GeminiProvider
        return GeminiProvider()

    if key.startswith("ollama:"):
        model = key[len("ollama:"):]
        from providers.ollama_provider import OllamaProvider
        # Heavy 14B model — same GPU but higher VRAM usage
        from runtime.config import MODEL_CODER_14B, MODEL_14B_HOST
        host = None
        if model == MODEL_CODER_14B:
            from runtime.config import MODEL_14B_HOST as h14
            host = h14
        return OllamaProvider(model, host=host)

    raise ValueError(f"Unknown provider key: {key!r}")


class ProviderRouter:
    """
    Route a task_type to the first available provider in its chain.

    Usage:
        router = ProviderRouter()
        provider = router.get_provider("hard-filter")
        result = provider.chat(messages)
        # provider.provider_id tells you which one ran
    """

    def __init__(self):
        self._table = ROUTING_TABLE

    def get_provider(self, task_type: str) -> Optional[BaseProvider]:
        """
        Walk the provider chain for task_type, return first available.
        Returns None if no provider in the chain is available.
        """
        chain = self._table.get(task_type)
        if chain is None:
            log.warning("No routing entry for task_type=%r — defaulting to deterministic", task_type)
            from providers.deterministic_provider import DeterministicProvider
            return DeterministicProvider()

        for key in chain:
            try:
                provider = _build_provider(key)
                if provider.is_available():
                    log.debug("Router: %s → %s", task_type, provider.provider_id)
                    return provider
                else:
                    log.debug("Router: %s skipped (unavailable)", key)
            except Exception as e:
                log.warning("Router: error checking provider %r: %s", key, e)

        log.warning("Router: no available provider for task_type=%r", task_type)
        return None

    def get_chain(self, task_type: str) -> list[str]:
        """Return the raw provider key chain for a task type."""
        return self._table.get(task_type, [])
