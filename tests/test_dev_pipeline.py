"""
Tests for the dev pipeline workers — deterministic paths only.
No real LLM calls.
"""

import pytest
from unittest.mock import patch, MagicMock


# ── TestRunner (always deterministic) ────────────────────────────────────────

def test_tester_valid_python():
    from runtime.workers.dev.tester import TestRunner
    result = TestRunner().run("x = 1 + 1\nprint(x)", "python")
    assert result["syntax_ok"] is True
    assert result["failed"] == 0
    assert result["provider_used"] == "deterministic"


def test_tester_invalid_python():
    from runtime.workers.dev.tester import TestRunner
    result = TestRunner().run("def bad_func(\n    print('oops')", "python")
    assert result["syntax_ok"] is False
    assert result["failed"] > 0
    assert len(result["errors"]) > 0


def test_tester_empty_code():
    from runtime.workers.dev.tester import TestRunner
    result = TestRunner().run("", "python")
    assert result["status"] == "failed"


def test_tester_detects_unsafe_code():
    from runtime.workers.dev.tester import TestRunner
    code = "import subprocess\nsubprocess.run(['ls'])"
    result = TestRunner().run(code, "python", safe_execute=False)
    assert result["safe_to_run"] is False


# ── CodeReviewer deterministic fallback ──────────────────────────────────────

def test_reviewer_fallback_when_no_llm():
    from runtime.workers.dev.reviewer import CodeReviewer
    with patch("providers.router.ProviderRouter.get_provider") as mock_get:
        from providers.deterministic_provider import DeterministicProvider
        mock_get.return_value = DeterministicProvider()
        result = CodeReviewer().run("x = 1\nprint(x)", "python")

    assert result["status"] == "success"
    assert result["verdict"] in ("pass", "needs_changes", "fail")
    assert result["provider_used"] == "deterministic"


def test_reviewer_catches_eval():
    from runtime.workers.dev.reviewer import CodeReviewer
    with patch("providers.router.ProviderRouter.get_provider") as mock_get:
        from providers.deterministic_provider import DeterministicProvider
        mock_get.return_value = DeterministicProvider()
        result = CodeReviewer().run("result = eval(user_input)", "python")

    # Should flag eval() usage
    high_issues = [i for i in result.get("issues", []) if i.get("severity") == "high"]
    assert len(high_issues) > 0


# ── CodeGenerator with mocked provider ───────────────────────────────────────

def test_generator_with_mock_provider():
    from runtime.workers.dev.generator import CodeGenerator
    mock_provider = MagicMock()
    mock_provider.provider_id = "ollama:qwen2.5-coder:7b-instruct-q4_K_M"
    mock_provider.chat.return_value = "def hello():\n    print('hello world')"

    with patch("providers.router.ProviderRouter.get_provider", return_value=mock_provider):
        result = CodeGenerator().run("write a hello world function", "python")

    assert result["status"] == "success"
    assert "hello" in result["code"]
    assert result["provider_used"] == "ollama:qwen2.5-coder:7b-instruct-q4_K_M"


def test_generator_fails_gracefully_when_no_provider():
    from runtime.workers.dev.generator import CodeGenerator
    with patch("providers.router.ProviderRouter.get_provider", return_value=None):
        result = CodeGenerator().run("write a hello world function", "python")

    assert result["status"] == "failed"
    assert result["code"] == ""


# ── DevSummarizer deterministic fallback ─────────────────────────────────────

def test_summarizer_deterministic_fallback():
    from runtime.workers.dev.summarizer import DevSummarizer
    with patch("providers.router.ProviderRouter.get_provider", return_value=None):
        result = DevSummarizer().run(
            spec="write a hello world function",
            generator_result={"status": "success", "code": "print('hi')", "language": "python"},
            reviewer_result={"verdict": "pass", "issues": [], "confidence": 0.9},
            tester_result={"syntax_ok": True, "tests_run": 1, "passed": 1, "failed": 0},
        )

    assert result["status"] == "success"
    assert len(result["summary"]) > 0
    assert result["overall_confidence"] > 0.0


# ── DevFinalizer ──────────────────────────────────────────────────────────────

def test_finalizer_writes_artifact(tmp_path, monkeypatch):
    from runtime.workers.dev.finalizer import DevFinalizer
    monkeypatch.setattr("runtime.workers.dev.finalizer.DIVISIONS_DIR", tmp_path)

    result = DevFinalizer().run(
        spec="write hello world",
        generator_result={"code": "print('hello')", "language": "python", "status": "success"},
        reviewer_result={"verdict": "pass", "issues": [], "confidence": 0.85},
        tester_result={"syntax_ok": True, "tests_run": 1, "passed": 1, "failed": 0},
        summarizer_result={"summary": "Great code", "overall_confidence": 0.85, "key_issues": []},
        task_id="test-001",
    )

    assert result["status"] == "success"
    assert result["artifact_ref"] != ""
    assert result["approval_required"] is True
