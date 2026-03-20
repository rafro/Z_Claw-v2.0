"""
Anthropic (Claude) provider — Tier 4, optional premium lane.

Auth priority:
  1. ANTHROPIC_API_KEY env var (explicit API key)
  2. OpenClaw auth profile (~/.openclaw/agents/main/agent/auth-profiles.json)
     Run: openclaw models auth setup-token  — to populate this.

Used only for: architecture-review, escalation-reason, dev-finalize (escalation),
and chat-operator fallback. Never the default for any task.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from providers.base import BaseProvider, ProviderError

log = logging.getLogger(__name__)

_OPENCLAW_AUTH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"


def _openclaw_token() -> str:
    """Read Claude session token from openclaw's stored auth profiles."""
    try:
        if not _OPENCLAW_AUTH.exists():
            return ""
        profiles = json.loads(_OPENCLAW_AUTH.read_text(encoding="utf-8")).get("profiles", {})
        for profile in profiles.values():
            if profile.get("provider") in ("anthropic", "claude"):
                return profile.get("token") or profile.get("key") or ""
    except Exception as e:
        log.debug("Could not read openclaw auth profile: %s", e)
    return ""


class AnthropicProvider(BaseProvider):
    """
    Claude via Anthropic API.
    Uses ANTHROPIC_API_KEY env var or your openclaw session token — no crash if absent.
    """

    DEFAULT_MODEL = "claude-sonnet-4-6"

    def __init__(self, model: str | None = None):
        self._model = model or os.getenv("CLAUDE_MODEL", self.DEFAULT_MODEL)

    def _get_key(self) -> str:
        return os.getenv("ANTHROPIC_API_KEY", "").strip() or _openclaw_token()

    @property
    def provider_id(self) -> str:
        return "claude"

    def is_available(self) -> bool:
        return bool(self._get_key())

    def chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 2048,
        json_mode: bool = False,
    ) -> str:
        key = self._get_key()
        if not key:
            raise ProviderError(
                "Claude unavailable — set ANTHROPIC_API_KEY or run: openclaw models auth setup-token",
                retryable=False,
            )

        try:
            import anthropic
        except ImportError:
            raise ProviderError("anthropic package not installed", retryable=False)

        try:
            system = ""
            conv_messages = []
            for m in messages:
                if m["role"] == "system":
                    system = m["content"]
                else:
                    conv_messages.append(m)

            client = anthropic.Anthropic(api_key=key)
            kwargs: dict[str, Any] = dict(
                model=self._model,
                max_tokens=max_tokens,
                messages=conv_messages,
                temperature=temperature,
            )
            if system:
                kwargs["system"] = system

            response = client.messages.create(**kwargs)
            return response.content[0].text.strip()

        except Exception as e:
            log.error("AnthropicProvider chat failed: %s", e)
            raise ProviderError(str(e), retryable=False) from e
