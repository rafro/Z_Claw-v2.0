"""
Deterministic provider — Tier 0, no LLM.
Returns None from chat(), forcing the caller into rule-based logic.
Used for tasks that should NEVER hit an LLM: job-intake (pure HTTP),
sentinel-health (pure Python checks), dev-test (static analysis first).
"""

from __future__ import annotations

from providers.base import BaseProvider, ProviderError


class DeterministicProvider(BaseProvider):
    """
    Pseudo-provider that signals 'use rule-based logic only'.

    Calling chat() raises ProviderError — callers should check
    provider_id == "deterministic" and branch to their rule-based path.
    Always available; never fails the availability check.
    """

    @property
    def provider_id(self) -> str:
        return "deterministic"

    def is_available(self) -> bool:
        return True

    def chat(self, messages, temperature=0.1, max_tokens=2048, json_mode=False) -> str:
        raise ProviderError(
            "DeterministicProvider does not run LLM — use rule-based logic",
            retryable=False,
        )
