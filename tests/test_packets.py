"""
Tests for schema validation — packets, tasks, approvals.
"""

import pytest
from schemas.packets import ExecutivePacket, TaskPacket
from schemas.tasks import Task, ApprovalRequest
from schemas.logs import AuditEntry


def test_executive_packet_required_fields():
    pkt = ExecutivePacket(
        division="opportunity",
        skill="job-intake",
        status="success",
        summary="Test summary",
    )
    assert pkt.division == "opportunity"
    assert pkt.skill == "job-intake"
    assert pkt.status == "success"
    assert pkt.escalate is False
    assert pkt.task_id is None
    assert pkt.urgency == "normal"
    assert pkt.provider_used == ""


def test_executive_packet_with_mc_fields():
    pkt = ExecutivePacket(
        division="dev",
        skill="dev-pipeline",
        status="success",
        summary="Generated hello world",
        task_id="abc-123",
        confidence=0.85,
        urgency="high",
        provider_used="ollama:qwen2.5-coder:7b-instruct-q4_K_M",
        approval_required=True,
        approval_status="pending",
    )
    assert pkt.task_id == "abc-123"
    assert pkt.confidence == 0.85
    assert pkt.approval_required is True


def test_executive_packet_to_dict():
    pkt = ExecutivePacket(
        division="trading", skill="market-scan", status="success", summary="ok"
    )
    d = pkt.to_dict()
    assert isinstance(d, dict)
    assert d["division"] == "trading"
    assert "task_id" in d


def test_task_packet_fields():
    tp = TaskPacket(
        task_id="t-001",
        worker="CodeGenerator",
        status="success",
        output={"code": "print('hello')"},
        provider_used="ollama:qwen2.5-coder:7b-instruct-q4_K_M",
        confidence=0.9,
    )
    assert tp.task_id == "t-001"
    assert tp.confidence == 0.9


def test_task_auto_generates_id():
    t = Task(type="hard-filter", division="opportunity")
    assert t.id != ""
    assert t.status == "queued"
    assert t.retry_count == 0


def test_approval_request_auto_id():
    req = ApprovalRequest(
        task_id="t-xyz",
        summary="Approve this",
        recommended_action="approve",
        urgency="high",
    )
    assert req.id != ""
    assert req.status == "pending"
    assert req.timeout_behavior == "reject"


def test_audit_entry_fields():
    entry = AuditEntry(
        event_type="task_submitted",
        agent="mission_control",
        data={"type": "hard-filter"},
    )
    assert entry.event_type == "task_submitted"
    assert "timestamp" in entry.model_dump()


def test_runtime_packet_build_has_mc_fields():
    """Ensure runtime/packet.py build() returns the new MC fields."""
    from runtime import packet
    pkt = packet.build(
        division="opportunity",
        skill="job-intake",
        status="success",
        summary="Test",
        task_id="t-abc",
        provider_used="ollama:qwen2.5:7b-instruct-q4_K_M",
        confidence=0.75,
        urgency="normal",
    )
    assert pkt["task_id"] == "t-abc"
    assert pkt["provider_used"] == "ollama:qwen2.5:7b-instruct-q4_K_M"
    assert pkt["confidence"] == 0.75
    assert pkt["urgency"] == "normal"
    assert "approval_required" in pkt
