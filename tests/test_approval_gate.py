"""
Tests for ApprovalGate — request, resolve, timeout behavior.
"""

import pytest
from pathlib import Path


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr("mission_control.approval.STATE_DIR", tmp_path)
    monkeypatch.setattr(
        "mission_control.approval.APPROVAL_FILE", tmp_path / "approval-queue.json"
    )
    from mission_control.approval import ApprovalGate
    return ApprovalGate()


def test_request_approval_creates_entry(gate):
    approval_id = gate.request_approval(
        task_id="t-001",
        summary="Review this code",
        recommended_action="approve",
        urgency="normal",
    )
    assert approval_id != ""
    pending = gate.list_pending()
    assert len(pending) == 1
    assert pending[0]["task_id"] == "t-001"
    assert pending[0]["status"] == "pending"


def test_resolve_approval_approve(gate):
    aid = gate.request_approval("t-002", "summary", "approve", "high")
    result = gate.resolve(aid, "approve", "matthew")
    assert result is True
    assert gate.get_status(aid) == "approved"
    assert gate.is_approved("t-002") is True
    assert len(gate.list_pending()) == 0


def test_resolve_approval_reject(gate):
    aid = gate.request_approval("t-003", "summary", "reject", "normal")
    gate.resolve(aid, "reject", "matthew")
    assert gate.get_status(aid) == "rejected"
    assert gate.is_approved("t-003") is False


def test_resolve_approval_escalate(gate):
    aid = gate.request_approval("t-004", "summary", "escalate", "critical")
    gate.resolve(aid, "escalate", "matthew")
    assert gate.get_status(aid) == "escalated"


def test_resolve_invalid_decision(gate):
    aid = gate.request_approval("t-005", "summary", "approve")
    result = gate.resolve(aid, "banana", "matthew")
    assert result is False
    assert gate.get_status(aid) == "pending"


def test_resolve_nonexistent_approval(gate):
    result = gate.resolve("nonexistent-id", "approve")
    assert result is False


def test_multiple_approvals_pending(gate):
    ids = [gate.request_approval(f"t-{i}", "s", "approve") for i in range(3)]
    assert len(gate.list_pending()) == 3
    gate.resolve(ids[1], "approve")
    assert len(gate.list_pending()) == 2


def test_block_until_resolved_fast(gate):
    """Test that block_until_resolved returns immediately once resolved."""
    import threading

    aid = gate.request_approval("t-fast", "summary", "approve")

    def resolve_after_delay():
        import time
        time.sleep(0.2)
        gate.resolve(aid, "approve")

    t = threading.Thread(target=resolve_after_delay)
    t.start()
    status = gate.block_until_resolved(aid, timeout_s=5, poll_interval=0.05)
    t.join()
    assert status == "approved"
