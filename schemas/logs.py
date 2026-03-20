"""
Pydantic models for audit and agent logs.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AuditEntry(BaseModel):
    timestamp: str = Field(default_factory=_utcnow)
    event_type: str            # "task_submitted" | "task_started" | "task_completed" | "task_failed" |
                               # "approval_requested" | "approval_resolved" | "provider_event" | "error"
    task_id: Optional[str] = None
    agent: str = ""            # which orchestrator/worker wrote this
    data: dict[str, Any] = {}


class AgentLog(BaseModel):
    timestamp: str = Field(default_factory=_utcnow)
    agent: str
    level: str = "info"        # "debug" | "info" | "warning" | "error"
    message: str
    task_id: Optional[str] = None
    extra: dict[str, Any] = {}


class ProviderEvent(BaseModel):
    timestamp: str = Field(default_factory=_utcnow)
    provider_id: str           # "ollama:qwen2.5:7b" | "gemini" | "claude" | "deterministic"
    event_type: str            # "available" | "unavailable" | "timeout" | "error" | "success"
    latency_ms: Optional[int] = None
    task_type: Optional[str] = None
    error: str = ""
