"""
Dev Automation Division Orchestrator.
Runs repo-monitor (Tier 2/14B → Tier 1/7B fallback).
Sends digest at 15:00 via packet flag.
"""

import logging
from datetime import datetime, timezone

from runtime.skills import repo_monitor
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log = logging.getLogger(__name__)


def run_repo_monitor(send_digest: bool = False) -> dict:
    """
    Run repo scan. send_digest=True at the 15:00 run to trigger Telegram output.
    """
    log.info("=== Dev Automation Division: repo-monitor run ===")

    result = repo_monitor.run()

    if result["status"] == "failed":
        pkt = packet.build(
            division="dev-automation",
            skill="repo-monitor",
            status="failed",
            summary="repo-monitor failed — gh CLI not authenticated.",
            escalate=True,
            escalation_reason=result.get("escalation_reason", ""),
        )
        packet.write(pkt)
        return pkt

    analysis  = result.get("analysis", {})
    flags     = result.get("flags", [])
    counts    = result.get("flag_counts", {})
    repos_n   = result.get("repos_checked", 0)

    summary = analysis.get("summary", f"{len(flags)} flags across {repos_n} repos.")

    # Build action items from high-priority findings
    action_items = []
    for finding in analysis.get("high_priority", [])[:5]:
        detail = finding if isinstance(finding, str) else finding.get("detail", str(finding))
        action_items.append(packet.action_item(
            detail, priority="high", requires_matthew=False
        ))
    for rec in analysis.get("recommendations", [])[:3]:
        action_items.append(packet.action_item(
            rec if isinstance(rec, str) else str(rec), priority="normal"
        ))

    pkt = packet.build(
        division="dev-automation",
        skill="repo-monitor",
        status=result["status"],
        summary=summary,
        action_items=action_items,
        metrics={
            "repos_checked": repos_n,
            "flags_high":    counts.get("high", 0),
            "flags_medium":  counts.get("medium", 0),
            "flags_low":     counts.get("low", 0),
            "send_digest":   send_digest,
        },
        artifact_refs=[{"bundle_id": "repo-scan-today", "location": "hot"}],
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    grant_skill_xp("repo-monitor")
    log.info(
        "Repo-monitor packet written. H=%d M=%d L=%d send_digest=%s",
        counts.get("high", 0), counts.get("medium", 0),
        counts.get("low", 0), send_digest
    )
    return pkt
