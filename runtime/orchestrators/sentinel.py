"""
Sentinel Division Orchestrator.
Extends op-sec with system health: provider monitoring, queue health, agent failure rates.
op_sec.py remains unchanged; sentinel.py adds the new Tier 0 health checks.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from runtime import packet
from runtime.tools.xp import grant_skill_xp
from runtime.workers.sentinel.provider_health import ProviderHealthWorker
from runtime.workers.sentinel.queue_monitor import QueueMonitor

log = logging.getLogger(__name__)


def run_provider_health() -> dict:
    """Check all configured providers and return a health packet."""
    result = ProviderHealthWorker().run()

    escalate = result["healthy_count"] == 0
    urgency = "critical" if escalate else ("high" if result["healthy_count"] < 2 else "normal")

    pkt = packet.build(
        division="sentinel",
        skill="provider-health",
        status="success",
        summary=result["summary"],
        metrics={
            "healthy_count": result["healthy_count"],
            "total_count": result["total_count"],
            "providers": result["providers"],
        },
        escalate=escalate,
        escalation_reason="All providers down" if escalate else "",
        provider_used="deterministic",
        urgency=urgency,
    )
    packet.write(pkt)
    grant_skill_xp("sentinel-health")
    return pkt


def run_queue_monitor() -> dict:
    """Check task queue for anomalies and return a health packet."""
    result = QueueMonitor().run()

    anomaly_count = len(result["anomalies"])
    escalate = anomaly_count > 0
    urgency = "high" if anomaly_count > 2 else ("normal" if not escalate else "normal")

    action_items = [
        packet.action_item(
            f"[{a['type']}] task {a['task_id']}: {a['detail']}",
            priority="high" if "stale" in a["type"] else "normal",
            requires_matthew=False,
        )
        for a in result["anomalies"][:5]
    ]

    pkt = packet.build(
        division="sentinel",
        skill="queue-monitor",
        status="success",
        summary=result["summary"],
        action_items=action_items,
        metrics={
            "queue_depth": result["queue_depth"],
            "running_count": result["running_count"],
            "failed_count": result["failed_count"],
            "anomaly_count": anomaly_count,
        },
        escalate=escalate,
        escalation_reason=f"{anomaly_count} queue anomalies detected" if escalate else "",
        provider_used="deterministic",
        urgency=urgency,
    )
    packet.write(pkt)
    return pkt


def run_agent_network_monitor() -> dict:
    """
    Check agent-network cycle state for staleness.
    Escalates if the cycle state file hasn't been updated in >6 hours —
    meaning the autonomous trading system may have stalled.
    """
    import glob as glob_mod
    from pathlib import Path

    AGENT_NETWORK_STATE = Path("C:/Users/Tyler/agent-network/state")
    STALE_THRESHOLD_HOURS = 6

    now = datetime.now(timezone.utc)
    checked_at = now.isoformat()

    # Find most recently modified cycle state file
    files = list(AGENT_NETWORK_STATE.glob("*_cycle_state.json")) if AGENT_NETWORK_STATE.exists() else []
    if not files:
        pkt = packet.build(
            division="sentinel",
            skill="agent-network-monitor",
            status="failed",
            summary="agent-network state directory not found or no cycle state files.",
            escalate=True,
            escalation_reason="agent-network cycle state missing — trading system may not be running.",
            provider_used="deterministic",
            urgency="high",
        )
        packet.write(pkt)
        return pkt

    latest = max(files, key=lambda p: p.stat().st_mtime)
    mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc)
    hours_since = (now - mtime).total_seconds() / 3600
    stale = hours_since > STALE_THRESHOLD_HOURS

    status = "stale" if stale else "active"
    summary = (
        f"agent-network last cycle: {hours_since:.1f}h ago ({latest.name}). "
        f"Status: {'STALE — may have stopped' if stale else 'ACTIVE'}."
    )

    escalate = stale
    escalation_reason = (
        f"agent-network cycle state is {hours_since:.1f}h old — "
        f"autonomous trading system may have stalled. Check {latest.name}."
    ) if stale else ""

    pkt = packet.build(
        division="sentinel",
        skill="agent-network-monitor",
        status="success",
        summary=summary,
        metrics={
            "state_file":      latest.name,
            "hours_since_update": round(hours_since, 2),
            "stale":           stale,
            "stale_threshold_hours": STALE_THRESHOLD_HOURS,
            "last_modified":   mtime.isoformat(),
            "checked_at":      checked_at,
        },
        escalate=escalate,
        escalation_reason=escalation_reason,
        provider_used="deterministic",
        urgency="high" if stale else "normal",
    )
    packet.write(pkt)
    log.info("agent-network monitor: %.1fh since last cycle — %s", hours_since, status)
    return pkt


def run_sentinel_digest() -> dict:
    """Unified sentinel digest: provider health + queue monitor + agent-network."""
    health_pkt  = run_provider_health()
    queue_pkt   = run_queue_monitor()
    an_pkt      = run_agent_network_monitor()

    providers_ok    = health_pkt["metrics"].get("healthy_count", 0)
    providers_total = health_pkt["metrics"].get("total_count", 0)
    anomalies       = queue_pkt["metrics"].get("anomaly_count", 0)
    an_stale        = an_pkt["metrics"].get("stale", False)

    escalate = health_pkt.get("escalate") or queue_pkt.get("escalate") or an_pkt.get("escalate")
    urgency  = "critical" if providers_ok == 0 else ("high" if (anomalies > 0 or an_stale) else "normal")

    summary = (
        f"System health: {providers_ok}/{providers_total} providers available. "
        f"Queue: {queue_pkt['metrics'].get('queue_depth', 0)} queued, "
        f"{queue_pkt['metrics'].get('failed_count', 0)} failed, "
        f"{anomalies} anomalies. "
        f"agent-network: {an_pkt['metrics'].get('hours_since_update', '?'):.1f}h since last cycle."
    )

    pkt = packet.build(
        division="sentinel",
        skill="sentinel-digest",
        status="success",
        summary=summary,
        metrics={
            "provider_health":   health_pkt["metrics"],
            "queue_health":      queue_pkt["metrics"],
            "agent_network":     an_pkt["metrics"],
        },
        escalate=escalate,
        escalation_reason=(
            health_pkt.get("escalation_reason")
            or queue_pkt.get("escalation_reason")
            or an_pkt.get("escalation_reason", "")
        ),
        provider_used="deterministic",
        urgency=urgency,
    )
    packet.write(pkt)
    return pkt
