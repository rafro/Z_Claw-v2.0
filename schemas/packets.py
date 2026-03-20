"""
Pydantic models for executive packets.
All division output to Mission Control goes through these schemas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


class ActionItem(BaseModel):
    priority: str = "normal"          # "low" | "normal" | "high"
    description: str
    requires_matthew: bool = False


class ExecutivePacket(BaseModel):
    division: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    skill: str
    status: str                        # "success" | "partial" | "failed"
    summary: str
    action_items: list[ActionItem] = []
    metrics: dict[str, Any] = {}
    artifact_refs: list[str] = []
    escalate: bool = False
    escalation_reason: str = ""

    # Mission Control fields (Phase 1 additions)
    task_id: Optional[str] = None
    confidence: Optional[float] = None   # 0.0–1.0
    urgency: str = "normal"              # "low" | "normal" | "high" | "critical"
    recommended_action: str = ""
    provider_used: str = ""              # e.g. "ollama:qwen2.5:7b" | "gemini" | "claude" | "deterministic"
    approval_required: bool = False
    approval_status: str = ""            # "pending" | "approved" | "rejected" | "escalated"

    def to_dict(self) -> dict:
        return self.model_dump()


class TaskPacket(BaseModel):
    """Lightweight result packet from a single worker (not a full division)."""
    task_id: str
    worker: str
    status: str                          # "success" | "failed" | "partial"
    output: Any = None
    error: str = ""
    provider_used: str = ""
    confidence: Optional[float] = None
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ProgressionPacket(BaseModel):
    """Tracks multi-step pipeline progression (e.g., dev pipeline)."""
    pipeline_id: str
    steps: list[str]
    completed: list[str] = []
    current_step: str = ""
    status: str = "in_progress"          # "in_progress" | "awaiting_approval" | "completed" | "failed"
    packets: dict[str, TaskPacket] = {}
    final_artifact: Optional[str] = None
