"""
Tests for sentinel workers — provider health and queue monitor.
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

import runtime.ollama_client  # ensure module is in sys.modules before patching


def test_provider_health_worker_returns_structure():
    """ProviderHealthWorker should return structured results without needing real Ollama."""
    with patch("runtime.ollama_client.is_available", return_value=False):
        from runtime.workers.sentinel.provider_health import ProviderHealthWorker
        result = ProviderHealthWorker().run()

    assert "providers" in result
    assert "healthy_count" in result
    assert "total_count" in result
    assert "summary" in result
    assert isinstance(result["providers"], dict)
    # Claude and Gemini should always be present
    assert "claude" in result["providers"]
    assert "gemini" in result["providers"]


def test_provider_health_no_api_keys(monkeypatch):
    """Without API keys, claude and gemini should be unavailable."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with patch("runtime.ollama_client.is_available", return_value=False):
        from runtime.workers.sentinel.provider_health import ProviderHealthWorker
        result = ProviderHealthWorker().run()

    assert result["providers"]["claude"]["status"] == "unavailable"
    assert result["providers"]["gemini"]["status"] == "unavailable"
    assert result["healthy_count"] == 0


def test_queue_monitor_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setattr("runtime.config.STATE_DIR", tmp_path)
    monkeypatch.setattr("runtime.workers.sentinel.queue_monitor.STATE_DIR", tmp_path)

    from runtime.workers.sentinel.queue_monitor import QueueMonitor
    result = QueueMonitor().run()

    assert "anomalies" in result
    assert "summary" in result
    assert result["queue_depth"] == 0


def test_queue_monitor_detects_stale_task(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta

    monkeypatch.setattr("runtime.config.STATE_DIR", tmp_path)
    monkeypatch.setattr("runtime.workers.sentinel.queue_monitor.STATE_DIR", tmp_path)

    # Write a task that has been "running" for 60 minutes
    stale_time = (datetime.now(timezone.utc) - timedelta(minutes=60)).isoformat()
    queue_data = {"tasks": [{
        "id": "stale-001",
        "type": "hard-filter",
        "division": "opportunity",
        "status": "running",
        "started_at": stale_time,
        "retry_count": 0,
    }]}
    (tmp_path / "task-queue.json").write_text(json.dumps(queue_data), encoding="utf-8")

    from runtime.workers.sentinel.queue_monitor import QueueMonitor
    result = QueueMonitor().run()

    assert len(result["anomalies"]) >= 1
    assert any(a["type"] == "stale_task" for a in result["anomalies"])
