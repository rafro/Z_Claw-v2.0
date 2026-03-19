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
    }


def write(pkt: dict) -> Path:
    """Write packet to divisions/{division}/packets/{skill}.json"""
    out = packet_path(pkt["division"], pkt["skill"])
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(pkt, f, indent=2, ensure_ascii=False)
    log.info("Packet written: %s", out)
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
