"""
Tests for MissionControl task lifecycle.
Uses a temp directory for state files — no real disk pollution.
"""

import json
import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch


@pytest.fixture
def mc(tmp_path, monkeypatch):
    """Return a MissionControl instance using a temp state directory."""
    monkeypatch.setattr("runtime.config.STATE_DIR", tmp_path)
    monkeypatch.setattr("mission_control.core.STATE_DIR", tmp_path)
    monkeypatch.setattr("mission_control.audit.LOGS_DIR", tmp_path)
    monkeypatch.setattr("mission_control.approval.STATE_DIR", tmp_path)
    monkeypatch.setattr("mission_control.core.TASK_QUEUE_FILE", tmp_path / "task-queue.json")
    monkeypatch.setattr("mission_control.approval.APPROVAL_FILE", tmp_path / "approval-queue.json")
    monkeypatch.setattr("mission_control.audit.AUDIT_FILE", tmp_path / "audit.jsonl")

    # Reload to get fresh state
    import importlib
    import mission_control.core as mc_mod
    importlib.reload(mc_mod)

    from mission_control.core import MissionControl
    return MissionControl()


def test_submit_and_get_task(mc):
    task_id = mc.submit_task("hard-filter", "opportunity", {"test": True})
    assert task_id != ""
    task = mc.get_task(task_id)
    assert task is not None
    assert task["type"] == "hard-filter"
    assert task["status"] == "queued"
    assert task["division"] == "opportunity"


def test_task_lifecycle(mc):
    task_id = mc.submit_task("market-scan", "trading")
    assert mc.get_task(task_id)["status"] == "queued"

    mc.start_task(task_id)
    assert mc.get_task(task_id)["status"] == "running"

    mc.complete_task(task_id, {"result": "ok"}, provider_used="ollama:qwen2.5:7b")
    task = mc.get_task(task_id)
    assert task["status"] == "completed"
    assert task["provider_used"] == "ollama:qwen2.5:7b"
    assert task["completed_at"] is not None


def test_fail_task(mc):
    task_id = mc.submit_task("health-logger", "personal")
    mc.start_task(task_id)
    mc.fail_task(task_id, "Ollama offline")
    task = mc.get_task(task_id)
    assert task["status"] == "failed"
    assert task["error"] == "Ollama offline"


def test_list_tasks_filter_by_status(mc):
    id1 = mc.submit_task("job-intake", "opportunity")
    id2 = mc.submit_task("market-scan", "trading")
    mc.start_task(id1)

    queued = mc.list_tasks(status="queued")
    running = mc.list_tasks(status="running")

    assert any(t["id"] == id2 for t in queued)
    assert any(t["id"] == id1 for t in running)


def test_multiple_tasks_persist(mc):
    ids = [mc.submit_task(f"task-{i}", "opportunity") for i in range(5)]
    all_tasks = mc.list_tasks()
    assert len(all_tasks) == 5
    assert all(any(t["id"] == tid for t in all_tasks) for tid in ids)
