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

    grant_skill_xp("sentinel-health")

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


def run_sentinel_digest() -> dict:
    """Unified sentinel digest: provider health + queue monitor."""
    health_pkt = run_provider_health()
    queue_pkt = run_queue_monitor()

    providers_ok = health_pkt["metrics"].get("healthy_count", 0)
    providers_total = health_pkt["metrics"].get("total_count", 0)
    anomalies = queue_pkt["metrics"].get("anomaly_count", 0)

    escalate = health_pkt.get("escalate") or queue_pkt.get("escalate")
    urgency = "critical" if providers_ok == 0 else ("high" if anomalies > 0 else "normal")

    summary = (
        f"System health: {providers_ok}/{providers_total} providers available. "
        f"Queue: {queue_pkt['metrics'].get('queue_depth', 0)} queued, "
        f"{queue_pkt['metrics'].get('failed_count', 0)} failed, "
        f"{anomalies} anomalies."
    )

    pkt = packet.build(
        division="sentinel",
        skill="sentinel-digest",
        status="success",
        summary=summary,
        metrics={
            "provider_health": health_pkt["metrics"],
            "queue_health": queue_pkt["metrics"],
        },
        escalate=escalate,
        escalation_reason=health_pkt.get("escalation_reason") or queue_pkt.get("escalation_reason", ""),
        provider_used="deterministic",
        urgency=urgency,
    )
    packet.write(pkt)
    return pkt
