"""
Executive packet builder and writer.
All division output to J_Claw goes through this module.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from runtime.config import packet_path, DIVISIONS_DIR

log = logging.getLogger(__name__)


def build(
    division: str,
    skill: str,
    status: str,               # "success" | "partial" | "failed"
    summary: str,
    action_items: list = None,
    metrics: dict = None,
    artifact_refs: list = None,
    escalate: bool = False,
    escalation_reason: str = "",
    # Mission Control fields
    task_id: str = "",
    confidence: float = None,  # 0.0–1.0
    urgency: str = "normal",   # "low" | "normal" | "high" | "critical"
    recommended_action: str = "",
    provider_used: str = "",   # "ollama:model" | "gemini" | "claude" | "deterministic"
    approval_required: bool = False,
    approval_status: str = "",
) -> dict:
    return {
        "division":          division,
        "generated_at":      datetime.now(timezone.utc).isoformat(),
        "skill":             skill,
        "status":            status,
        "summary":           summary,
        "action_items":      action_items or [],
        "metrics":           metrics or {},
        "artifact_refs":     artifact_refs or [],
        "escalate":          escalate,
        "escalation_reason": escalation_reason,
        # Mission Control fields
        "task_id":           task_id,
        "confidence":        confidence,
        "urgency":           urgency,
        "recommended_action": recommended_action,
        "provider_used":     provider_used,
        "approval_required": approval_required,
        "approval_status":   approval_status,
    }


def write(pkt: dict) -> Path:
    """Write packet to divisions/{division}/packets/{skill}.json.
    If escalate=True, also sends a Discord DM to Tyler.
    """
    out = packet_path(pkt["division"], pkt["skill"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pkt, f, indent=2, ensure_ascii=False)
    log.info("Packet written: %s", out)

    if pkt.get("escalate") and pkt.get("escalation_reason"):
        try:
            from runtime.tools.discord_notify import notify_escalation
            notify_escalation(
                division=pkt["division"],
                skill=pkt["skill"],
                reason=pkt["escalation_reason"],
                action_items=pkt.get("action_items"),
            )
        except Exception as e:
            log.warning("Discord notification failed (non-fatal): %s", e)

    return out


def read(division: str, skill: str) -> Optional[dict]:
    path = packet_path(division, skill)
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def action_item(description: str, priority: str = "normal",
                requires_matthew: bool = False) -> dict:
    return {
        "priority":         priority,
        "description":      description,
        "requires_matthew": requires_matthew,
    }


def read_fresh(division: str, skill: str, max_age_minutes: int = 60):
    """Read a packet only if generated within max_age_minutes. Returns None if stale."""
    from runtime.tools.artifact_hydration import read_fresh as _read_fresh
    return _read_fresh(division, skill, max_age_minutes)


def job_action_item(job: dict) -> dict:
    pay = ""
    if job.get("pay_min"):
        pay = f"${job['pay_min']:,}"
        if job.get("pay_max"):
            pay += f"–${job['pay_max']:,}"
    elif job.get("salary_raw"):
        pay = job["salary_raw"]
    else:
        pay = "Pay unspecified"

    score = job.get("score_composite")
    score_str = f" | Fit: {score:.1f}/10" if score else ""
    resume = job.get("resume", "").capitalize() if job.get("resume") else ""

    desc = (
        f"[TIER {job['tier']}] {job['title']} — {job.get('company','?')}"
        f" | {job['location']} | {pay}{score_str}"
        f" | Resume: {resume} | {job['url']}"
    )
    return action_item(desc, priority="high", requires_matthew=True)


def read_fresh(division: str, skill: str, max_age_minutes: int = 60):
    """Read a packet only if generated within max_age_minutes. Returns None if stale."""
    from runtime.tools.artifact_hydration import read_fresh as _read_fresh
    return _read_fresh(division, skill, max_age_minutes)
