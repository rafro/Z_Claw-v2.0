"""
Tests for the provider abstraction layer.
All tests are smoke tests — no real LLM calls needed.
"""

import pytest
from unittest.mock import patch, MagicMock


# ── Provider instantiation ────────────────────────────────────────────────────

def test_deterministic_provider_always_available():
    from providers.deterministic_provider import DeterministicProvider
    p = DeterministicProvider()
    assert p.is_available() is True
    assert p.provider_id == "deterministic"


def test_deterministic_provider_chat_raises():
    from providers.deterministic_provider import DeterministicProvider
    from providers.base import ProviderError
    p = DeterministicProvider()
    with pytest.raises(ProviderError):
        p.chat([{"role": "user", "content": "hi"}])


def test_anthropic_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with patch("providers.anthropic_provider._openclaw_token", return_value=""):
        from providers.anthropic_provider import AnthropicProvider
        p = AnthropicProvider()
        assert p.is_available() is False
        assert p.provider_id == "claude"


def test_anthropic_provider_available_with_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-key")
    from providers.anthropic_provider import AnthropicProvider
    p = AnthropicProvider()
    assert p.is_available() is True


def test_gemini_provider_unavailable_without_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from providers.gemini_provider import GeminiProvider
    p = GeminiProvider()
    assert p.is_available() is False
    assert p.provider_id == "gemini"


def test_ollama_provider_has_correct_id():
    from providers.ollama_provider import OllamaProvider
    p = OllamaProvider("qwen2.5:7b-instruct-q4_K_M")
    assert p.provider_id == "ollama:qwen2.5:7b-instruct-q4_K_M"


# ── ProviderRouter ────────────────────────────────────────────────────────────

def test_router_returns_deterministic_for_job_intake():
    from providers.router import ProviderRouter
    router = ProviderRouter()
    chain = router.get_chain("job-intake")
    assert chain == ["deterministic"]


def test_router_returns_deterministic_for_sentinel_health():
    from providers.router import ProviderRouter
    router = ProviderRouter()
    chain = router.get_chain("sentinel-health")
    assert chain == ["deterministic"]


def test_router_chat_operator_has_claude_fallback():
    from providers.router import ProviderRouter
    router = ProviderRouter()
    chain = router.get_chain("chat-operator")
    assert "claude" in chain


def test_router_health_logger_no_cloud_fallback():
    """health-logger must NEVER have claude or gemini in its chain (privacy rule)."""
    from providers.router import ProviderRouter
    router = ProviderRouter()
    chain = router.get_chain("health-logger")
    assert "claude" not in chain
    assert "gemini" not in chain


def test_router_gets_deterministic_when_ollama_unavailable(monkeypatch):
    """When Ollama is down and no API keys set, hard-filter returns deterministic."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with patch("providers.ollama_provider.OllamaProvider.is_available", return_value=False):
        from providers.router import ProviderRouter
        router = ProviderRouter()
        provider = router.get_provider("hard-filter")
        assert provider is not None
        assert provider.provider_id == "deterministic"


def test_router_skips_claude_when_key_absent(monkeypatch):
    """debug-agent chain: ollama:coder-14b → gemini → claude. With no keys, should skip all API."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with patch("providers.ollama_provider.OllamaProvider.is_available", return_value=False), \
         patch("providers.anthropic_provider._openclaw_token", return_value=""):
        from providers.router import ProviderRouter
        router = ProviderRouter()
        provider = router.get_provider("debug-agent")
        # Should return None since all providers are down
        assert provider is None


def test_router_unknown_task_returns_deterministic():
    from providers.router import ProviderRouter
    router = ProviderRouter()
    provider = router.get_provider("completely-unknown-task-xyz")
    assert provider is not None
    assert provider.provider_id == "deterministic"
