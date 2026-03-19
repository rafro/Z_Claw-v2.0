"""
Personal Division Orchestrator.
Runs health-logger, perf-correlation, realm-keeper notifications.
Health data stays strictly local — Tier 1 Llama 3.1 8B, no external calls.
"""

import logging

from runtime.skills import health_logger, perf_correlation
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log = logging.getLogger(__name__)


def run_health_logger(reply_text: str) -> dict:
    """
    Parse and store health check-in reply.
    reply_text: the raw Telegram message from Matthew.
    """
    log.info("=== Personal Division: health-logger run ===")

    result = health_logger.run(reply_text)
    entry  = result.get("entry", {})

    # Summary for J_Claw — safe for Telegram (no raw medication data)
    summary = result.get("summary", "Health entry processed.")

    action_items = []
    if result.get("missing_required"):
        action_items.append(packet.action_item(
            "Health log incomplete — sleep_hours missing. Send follow-up.",
            priority="normal", requires_matthew=False,
        ))

    pkt = packet.build(
        division="personal",
        skill="health-logger",
        status=result["status"],
        summary=summary,
        action_items=action_items,
        metrics={
            "health_logged":  not entry.get("skipped", False),
            "sleep_hours":    entry.get("sleep_hours"),
            "sleep_quality":  entry.get("sleep_quality"),
            "skipped":        entry.get("skipped", False),
            "model_available": result.get("model_available", True),
        },
        artifact_refs=[{
            "bundle_id": f"health-{entry.get('date', 'today')}",
            "location":  "hot",
        }],
    )

    packet.write(pkt)
    grant_skill_xp("health-logger")
    log.info("Health-logger packet written. Skipped=%s", entry.get("skipped"))
    return pkt


def run_perf_correlation() -> dict:
    """Cross-reference health vs trading performance."""
    log.info("=== Personal Division: perf-correlation run ===")

    result = perf_correlation.run()

    pkt = packet.build(
        division="personal",
        skill="perf-correlation",
        status=result["status"],
        summary=result.get("summary", "No patterns detected."),
        metrics={"data_points": result.get("data_points", 0)},
    )

    packet.write(pkt)
    if result["status"] == "success":
        grant_skill_xp("perf-correlation")
    log.info("Perf-correlation packet written.")
    return pkt
