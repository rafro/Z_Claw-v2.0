"""
ProviderRouter — maps task_type → ordered provider chain.
Walks the chain until a provider reports is_available().

This is the single source of truth for model routing.
runtime/config.py SKILL_MODELS is deprecated; import from here instead.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from providers.base import BaseProvider
from providers.capture import CaptureProvider

log = logging.getLogger(__name__)

# ── Adapter support ──────────────────────────────────────────────────────────
_ADAPTER_STATE = Path(__file__).resolve().parent.parent / "state" / "active-adapters.json"

# Map task_type prefixes to fine-tuning domains.
_DOMAIN_PREFIXES: list[tuple[str, str]] = [
    ("trading",   "trading"),
    ("market",    "trading"),
    ("security",  "opsec"),
    ("threat",    "opsec"),
    ("breach",    "opsec"),
    ("opsec",     "opsec"),
    ("cred",      "opsec"),
    ("privacy",   "opsec"),
    ("dev",       "dev"),
    ("repo",      "dev"),
    ("debug",     "dev"),
    ("refactor",  "dev"),
    ("doc",       "dev"),
    ("health",    "personal"),
    ("perf",      "personal"),
    ("burnout",   "personal"),
    ("funding",   "opportunity"),
    ("hard-filter", "opportunity"),
    ("job",       "opportunity"),
]


def _load_active_adapters() -> dict[str, str]:
    """Read state/active-adapters.json → {domain: adapter_path}. Returns {} if missing."""
    try:
        return json.loads(_ADAPTER_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _domain_for_task_type(task_type: str) -> Optional[str]:
    """Derive the fine-tuning domain from a task_type string."""
    for prefix, domain in _DOMAIN_PREFIXES:
        if task_type.startswith(prefix):
            return domain
    return None

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
    "funding-finder":        ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],

    # ── Trading ──────────────────────────────────────────────────────────────
    "market-scan":           ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],
    "trading-report":        ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],

    # ── Personal (health-logger is LOCAL ONLY — never cloud fallback) ────────
    "health-logger":         ["ollama:llama3.2:3b"],                        # LOCAL ONLY
    "perf-correlation":      ["ollama:qwen2.5:7b-instruct-q4_K_M"],         # LOCAL ONLY
    "burnout-monitor":       ["ollama:qwen2.5:7b-instruct-q4_K_M"],         # LOCAL ONLY

    # ── Dev Automation ───────────────────────────────────────────────────────
    "repo-monitor":          ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "groq", "deepseek"],
    "debug-agent":           ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "groq", "deepseek"],
    "refactor-scan":         ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "groq", "deepseek"],
    "doc-update":            ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "groq", "deepseek"],
    "opsec-scan":            ["ollama:qwen2.5-coder:7b-instruct-q4_K_M"],    # LOCAL ONLY — security data

    # ── Dev Pipeline ─────────────────────────────────────────────────────────
    "dev-generate":          ["ollama:qwen2.5-coder:7b-instruct-q4_K_M",    "groq", "deepseek"],
    "dev-review":            ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "groq", "deepseek"],
    "dev-test":              ["deterministic", "ollama:qwen2.5-coder:7b-instruct-q4_K_M"],
    "dev-summarize":         ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],
    "dev-finalize":          ["ollama:qwen2.5-coder:14b-instruct-q4_K_M",   "groq", "deepseek"],

    # ── Architecture / Escalation — Groq 70B primary, DeepSeek backup ───────
    "architecture-review":   ["groq", "deepseek", "gemini"],
    "escalation-reason":     ["groq", "deepseek", "gemini"],

    # ── OP-Sec (ALL LOCAL ONLY — security/network/credential data never leaves system) ──
    "device-posture":        ["deterministic"],                              # LOCAL ONLY
    "breach-check":          ["deterministic"],                              # LOCAL ONLY
    "threat-surface":        ["ollama:qwen2.5-coder:7b-instruct-q4_K_M"],   # LOCAL ONLY
    "cred-audit":            ["ollama:qwen2.5-coder:7b-instruct-q4_K_M"],   # LOCAL ONLY
    "privacy-scan":          ["ollama:qwen2.5:7b-instruct-q4_K_M"],         # LOCAL ONLY
    "security-scan":         ["ollama:qwen2.5-coder:7b-instruct-q4_K_M"],   # LOCAL ONLY
    "opsec-digest":          ["ollama:qwen2.5:7b-instruct-q4_K_M"],         # LOCAL ONLY

    # ── Sentinel (health monitoring) ─────────────────────────────────────────
    "sentinel-health":       ["deterministic"],
    "queue-monitor":         ["deterministic"],
    "agent-health":          ["deterministic"],

    # ── Operator Chat ────────────────────────────────────────────────────────
    "chat-operator":         ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],

    # ── Digest synthesis ─────────────────────────────────────────────────────
    "dev-digest":            ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],
    "personal-digest":       ["ollama:qwen2.5:7b-instruct-q4_K_M",          "groq", "deepseek"],
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

    if key == "groq":
        from providers.groq_provider import GroqProvider
        return GroqProvider()

    if key == "deepseek":
        from providers.deepseek_provider import DeepSeekProvider
        return DeepSeekProvider()

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

        If a fine-tuned LoRA adapter is active for the task's domain, the
        selected provider is annotated with ``_adapter_path`` so backends
        that support adapter loading (e.g. Ollama) can use it.
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

                    # ── Adapter annotation ────────────────────────────────
                    domain = _domain_for_task_type(task_type)
                    if domain:
                        adapters = _load_active_adapters()
                        adapter_path = adapters.get(domain)
                        if adapter_path:
                            provider._adapter_path = adapter_path
                            log.info("Using fine-tuned adapter for %s: %s", domain, adapter_path)

                    return CaptureProvider(provider, task_type=task_type)
                else:
                    log.debug("Router: %s skipped (unavailable)", key)
            except Exception as e:
                log.warning("Router: error checking provider %r: %s", key, e)

        log.warning("Router: no available provider for task_type=%r", task_type)
        return None

    def get_chain(self, task_type: str) -> list[str]:
        """Return the raw provider key chain for a task type."""
        return self._table.get(task_type, [])
