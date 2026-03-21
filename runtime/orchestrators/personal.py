"""
Personal Division Orchestrator — LLM agent (Qwen2.5 7B, strictly local).
Health data never leaves the machine — no external API calls, ever.
The orchestrator synthesizes across health, performance, and burnout signals
to give J_Claw a single coherent picture of Matthew's daily state.
"""

import logging

from runtime.config import SKILL_MODELS, OLLAMA_HOST
from runtime.ollama_client import chat, is_available
from runtime.skills import health_logger, perf_correlation, burnout_monitor
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log   = logging.getLogger(__name__)
# Personal division uses local-only model — no API fallback
MODEL = SKILL_MODELS["health-logger"]


# ── Orchestrator reasoning ─────────────────────────────────────────────────────

def _synthesize_personal_state(
    health_pkt: dict | None,
    perf_pkt: dict | None,
    burnout_pkt: dict | None,
) -> str:
    """
    Cross-skill synthesis: combine health log, performance patterns, and burnout signals.
    Produces a holistic assessment of Matthew's current state — safe for Telegram.
    Never includes raw medication data or detailed health fields in output.
    """
    # Build safe context (no raw health values that shouldn't leave device)
    health_summary  = health_pkt.get("summary", "No health data.") if health_pkt else "No health data."
    perf_summary    = perf_pkt.get("summary", "No performance patterns.") if perf_pkt else "No performance patterns."
    burnout_summary = burnout_pkt.get("summary", "No burnout data.") if burnout_pkt else "No burnout data."
    burnout_level   = burnout_pkt.get("metrics", {}).get("avg_sleep_hours") if burnout_pkt else None

    if not is_available(MODEL):
        parts = [s for s in [health_summary, perf_summary, burnout_summary] if s and "No " not in s]
        return " | ".join(parts) if parts else "Personal data logged."

    context = (
        f"Health check-in: {health_summary}\n"
        f"Performance patterns: {perf_summary}\n"
        f"Burnout monitor: {burnout_summary}"
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Personal Division orchestrator for J_Claw. "
                "Given today's health check-in, performance patterns, and burnout signals, "
                "write a 2-sentence executive summary of Matthew's current state. "
                "Focus on: sleep quality, trading readiness, and any risks to watch. "
                "Do NOT include specific medication names, dosages, or detailed health metrics. "
                "Safe for Telegram. Start directly with the first sentence — no preamble, no 'Here is', no labels."
            ),
        },
        {"role": "user", "content": context},
    ]
    try:
        result = chat(MODEL, messages, temperature=0.2, max_tokens=150)
        # Strip common preamble patterns (Llama 3.1 8B habit)
        lines = result.strip().splitlines()
        if lines and lines[0].rstrip().endswith(":"):
            result = "\n".join(lines[1:]).lstrip()
        return result
    except Exception as e:
        log.warning("personal orchestrator synthesis failed: %s", e)
        return health_summary


# ── Individual skill runners ───────────────────────────────────────────────────

def run_health_logger(reply_text: str) -> dict:
    """Parse and store Matthew's health check-in reply from Telegram."""
    log.info("=== Personal Division: health-logger run ===")

    result = health_logger.run(reply_text)
    entry  = result.get("entry", {})
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
            "health_logged":   not entry.get("skipped", False),
            "sleep_hours":     entry.get("sleep_hours"),
            "sleep_quality":   entry.get("sleep_quality"),
            "skipped":         entry.get("skipped", False),
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
    """Cross-reference health vs trading performance over 14-day window."""
    log.info("=== Personal Division: perf-correlation run ===")

    result = perf_correlation.run()

    # Orchestrator adds context from burnout monitor if available
    burnout_pkt = packet.read("personal", "burnout-monitor")
    summary = result.get("summary", "No patterns detected.")

    if burnout_pkt and burnout_pkt.get("metrics", {}).get("avg_sleep_hours"):
        avg_sleep = burnout_pkt["metrics"]["avg_sleep_hours"]
        if avg_sleep and avg_sleep < 6.5:
            summary += f" Note: avg sleep {avg_sleep:.1f}h — patterns may reflect fatigue."

    pkt = packet.build(
        division="personal",
        skill="perf-correlation",
        status=result["status"],
        summary=summary,
        metrics={
            "data_points":    result.get("data_points", 0),
            "burnout_context": bool(burnout_pkt),
        },
    )

    packet.write(pkt)
    if result["status"] == "success":
        grant_skill_xp("perf-correlation")
    log.info("Perf-correlation packet written.")
    return pkt


def run_burnout_monitor() -> dict:
    """Daily burnout check — sleep trends, skipped logs, emotional state."""
    log.info("=== Personal Division: burnout-monitor run ===")

    result = burnout_monitor.run()
    level  = result.get("level", "ok")

    pkt = packet.build(
        division="personal",
        skill="burnout-monitor",
        status="success" if level == "ok" else "partial",
        summary=result.get("recommendation", "Burnout check complete."),
        metrics=result.get("metrics", {}),
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    grant_skill_xp("burnout-monitor")
    log.info("Burnout-monitor packet written. Level=%s", level)
    return pkt


def run_personal_digest() -> dict:
    """
    Daily 21:30 — orchestrator synthesizes across ALL personal division skills.
    Reads health-logger, perf-correlation, and burnout-monitor packets,
    produces a single cross-skill executive summary for the nightly briefing.
    This is where the Personal Division orchestrator earns its LLM tier.
    """
    log.info("=== Personal Division: personal-digest synthesis ===")

    health_pkt  = packet.read("personal", "health-logger")
    perf_pkt    = packet.read("personal", "perf-correlation")
    burnout_pkt = packet.read("personal", "burnout-monitor")

    synthesis = _synthesize_personal_state(health_pkt, perf_pkt, burnout_pkt)

    # Aggregate escalation signals
    escalate = any(
        p.get("escalate", False)
        for p in [health_pkt, perf_pkt, burnout_pkt]
        if p
    )
    escalation_reasons = [
        p.get("escalation_reason", "")
        for p in [health_pkt, perf_pkt, burnout_pkt]
        if p and p.get("escalation_reason")
    ]

    pkt = packet.build(
        division="personal",
        skill="personal-digest",
        status="success",
        summary=synthesis,
        metrics={
            "health_logged":     bool(health_pkt and not health_pkt.get("metrics", {}).get("skipped")),
            "patterns_found":    bool(perf_pkt and "no patterns" not in perf_pkt.get("summary", "").lower()),
            "burnout_level":     burnout_pkt.get("summary", "ok") if burnout_pkt else "unknown",
            "data_sources":      sum(1 for p in [health_pkt, perf_pkt, burnout_pkt] if p),
        },
        escalate=escalate,
        escalation_reason=" | ".join(escalation_reasons) if escalation_reasons else "",
    )

    packet.write(pkt)
    grant_skill_xp("personal-digest")
    log.info("Personal digest packet written. Escalate=%s", escalate)
    return pkt
