"""
application-tracker skill — Tier 0 (pure Python) + Tier 1 LLM summary.
Tracks job application status from the applications pipeline.
Runs daily at 10:00 AM (after job-intake at 9:00 AM).

Reads applications.json pipeline, categorizes by status, tracks:
  - Total applications sent
  - Applications awaiting reply (pending_reply / applied)
  - Applications requiring follow-up
  - Response rate and time-to-response estimates
"""

import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

from runtime.config import STATE_DIR, ROOT
from runtime.tools.state import load_applications

log        = logging.getLogger(__name__)
HOT_DIR    = ROOT / "divisions" / "opportunity" / "hot"
PACKET_DIR = ROOT / "divisions" / "opportunity" / "packets"

# Days after application to suggest a follow-up
FOLLOWUP_THRESHOLD_DAYS = 7
# Days after which an application is considered "cold" (no response)
COLD_THRESHOLD_DAYS = 30


def _days_since(date_str: str) -> int:
    """Calculate days since a given ISO date string."""
    try:
        if "T" in date_str:
            d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        else:
            d = date.fromisoformat(date_str)
        return (date.today() - d).days
    except Exception:
        return 0


def _analyze_pipeline(apps: dict) -> dict:
    """Pure Python analysis of the application pipeline."""
    pipeline = apps.get("pipeline", [])

    # Status categories
    applied = []
    pending_review = []
    interviewing = []
    rejected = []
    accepted = []
    withdrawn = []
    follow_up_needed = []
    cold = []

    for job in pipeline:
        status = (job.get("status") or "").lower().strip()
        applied_at = job.get("applied_at") or job.get("created_at") or job.get("found_at") or ""

        if status in ("applied", "sent"):
            applied.append(job)
            days = _days_since(applied_at) if applied_at else 0
            if days >= FOLLOWUP_THRESHOLD_DAYS and days < COLD_THRESHOLD_DAYS:
                follow_up_needed.append(job)
            elif days >= COLD_THRESHOLD_DAYS:
                cold.append(job)
        elif status in ("pending_review", "pending", "new"):
            pending_review.append(job)
        elif status in ("interviewing", "interview", "phone_screen", "technical"):
            interviewing.append(job)
        elif status in ("rejected", "declined", "no_response"):
            rejected.append(job)
        elif status in ("accepted", "offer", "hired"):
            accepted.append(job)
        elif status in ("withdrawn", "cancelled"):
            withdrawn.append(job)

    total_sent = len(applied) + len(interviewing) + len(rejected) + len(accepted) + len(withdrawn)
    awaiting_reply = len(applied) + len(interviewing)

    # Response rate (applications that got a response vs total sent)
    responded = len(interviewing) + len(rejected) + len(accepted)
    response_rate = round(responded / total_sent * 100, 1) if total_sent > 0 else None

    return {
        "total_pipeline": len(pipeline),
        "total_sent": total_sent,
        "applications_sent": total_sent,
        "pending_review": len(pending_review),
        "awaiting_reply": awaiting_reply,
        "pending_reply": awaiting_reply,
        "interviewing": len(interviewing),
        "rejected": len(rejected),
        "accepted": len(accepted),
        "withdrawn": len(withdrawn),
        "follow_up_needed": len(follow_up_needed),
        "cold_applications": len(cold),
        "response_rate": response_rate,
        "follow_up_jobs": [
            {
                "title": j.get("title", "?"),
                "company": j.get("company", "?"),
                "applied_at": j.get("applied_at", "?"),
                "days_since": _days_since(j.get("applied_at") or j.get("created_at") or ""),
            }
            for j in follow_up_needed[:5]
        ],
        "active_interviews": [
            {
                "title": j.get("title", "?"),
                "company": j.get("company", "?"),
                "status": j.get("status", "?"),
            }
            for j in interviewing[:5]
        ],
    }


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    apps = load_applications()
    analysis = _analyze_pipeline(apps)

    # Build summary
    parts = []
    parts.append(f"{analysis['total_sent']} application(s) sent")
    parts.append(f"{analysis['awaiting_reply']} awaiting reply")

    if analysis["interviewing"] > 0:
        parts.append(f"{analysis['interviewing']} in interview stage")
    if analysis["follow_up_needed"] > 0:
        parts.append(f"{analysis['follow_up_needed']} need follow-up")
    if analysis["cold_applications"] > 0:
        parts.append(f"{analysis['cold_applications']} gone cold (>{COLD_THRESHOLD_DAYS}d)")
    if analysis["response_rate"] is not None:
        parts.append(f"response rate: {analysis['response_rate']}%")

    summary = "Application tracker: " + ", ".join(parts) + "."

    # Build action items
    action_items = []
    for job in analysis.get("follow_up_jobs", []):
        action_items.append({
            "priority": "normal",
            "description": (
                f"Follow up: {job['title']} at {job['company']} "
                f"(applied {job['days_since']}d ago)"
            ),
            "requires_matthew": True,
        })

    if analysis["pending_review"] > 0:
        action_items.append({
            "priority": "normal",
            "description": f"{analysis['pending_review']} job(s) awaiting review — score and decide.",
            "requires_matthew": True,
        })

    # Escalate if there are active interviews or many follow-ups needed
    escalate = analysis["interviewing"] > 0
    escalation_reason = ""
    if analysis["interviewing"] > 0:
        escalation_reason = f"{analysis['interviewing']} active interview(s) — check for scheduling/prep needs."

    packet = {
        "status":            "success",
        "escalate":          escalate,
        "escalation_reason": escalation_reason,
        "summary":           summary,
        "action_items":      action_items,
        "scanned_at":        datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "applications_sent": analysis["total_sent"],
            "total_sent":        analysis["total_sent"],
            "pending_reply":     analysis["awaiting_reply"],
            "awaiting_reply":    analysis["awaiting_reply"],
            "pending_review":    analysis["pending_review"],
            "interviewing":      analysis["interviewing"],
            "rejected":          analysis["rejected"],
            "accepted":          analysis["accepted"],
            "follow_up_needed":  analysis["follow_up_needed"],
            "cold_applications": analysis["cold_applications"],
            "response_rate":     analysis["response_rate"],
            "total_pipeline":    analysis["total_pipeline"],
        },
    }

    with open(PACKET_DIR / "application-tracker.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet
