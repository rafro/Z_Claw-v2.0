"""
Pydantic models for tasks and approvals.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field
import uuid


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class Task(BaseModel):
    id: str = Field(default_factory=_new_id)
    type: str                           # e.g. "hard-filter", "dev-pipeline", "market-scan"
    division: str                       # e.g. "opportunity", "dev", "sentinel"
    payload: dict[str, Any] = {}
    status: str = "queued"              # "queued" | "running" | "completed" | "failed" | "awaiting_approval"
    submitted_at: str = Field(default_factory=_utcnow)
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    result: Optional[Any] = None
    error: Optional[str] = None
    provider_used: str = ""
    retry_count: int = 0
    max_retries: int = 2


class TaskStatus(BaseModel):
    id: str
    status: str
    division: str
    type: str
    submitted_at: str
    completed_at: Optional[str] = None
    error: Optional[str] = None


class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=_new_id)
    task_id: str
    summary: str
    recommended_action: str
    urgency: str = "normal"             # "low" | "normal" | "high" | "critical"
    status: str = "pending"             # "pending" | "approved" | "rejected" | "escalated" | "timed_out"
    requested_at: str = Field(default_factory=_utcnow)
    resolved_at: Optional[str] = None
    resolved_by: str = ""               # "matthew" | "timeout" | "system"
    timeout_behavior: str = "reject"    # default on timeout: "approve" | "reject"
