"""
weekly-retrospective skill — Tier 1 LLM (Qwen2.5 7B local).
Monday morning pattern analysis across all divisions.
Reads the past week's packets from every division, identifies cross-cutting
patterns, wins, risks, and recommends focus areas for the coming week.

Data stays local — no external API calls. Health data is summarized
(no raw values) to keep the output safe for Telegram.
"""

import json
import logging
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

from runtime.config import SKILL_MODELS, OLLAMA_HOST, ROOT
from runtime.ollama_client import chat, is_available
from runtime.tools.state import load_health_log, load_trade_log
from runtime import packet as packet_io

log        = logging.getLogger(__name__)
MODEL      = SKILL_MODELS.get("perf-correlation", "qwen2.5:7b-instruct-q4_K_M")
HOT_DIR    = ROOT / "divisions" / "personal" / "hot"
PACKET_DIR = ROOT / "divisions" / "personal" / "packets"

LOOKBACK_DAYS = 7

# Division packets to check for weekly summary
DIVISION_PACKETS = {
    "opportunity": ["job-intake", "funding-finder", "application-tracker"],
    "trading":     ["trading-report", "market-scan"],
    "personal":    ["health-logger", "burnout-monitor", "perf-correlation"],
    "op-sec":      ["device-posture", "threat-surface", "breach-check",
                    "cred-audit", "privacy-scan", "network-monitor", "opsec-digest"],
    "dev-automation": ["repo-monitor", "refactor-scan", "doc-update",
                       "artifact-manager", "dev-digest"],
    "production":  ["production-digest", "asset-catalog"],
    "sentinel":    ["provider-health", "queue-monitor", "sentinel-digest"],
}


def _read_division_packets() -> dict:
    """Read the most recent packet from each division skill."""
    results = {}
    for division, skills in DIVISION_PACKETS.items():
        div_data = {}
        for skill in skills:
            pkt = packet_io.read(division, skill)
            if pkt:
                div_data[skill] = {
                    "status":    pkt.get("status", "?"),
                    "summary":   pkt.get("summary", ""),
                    "escalate":  pkt.get("escalate", False),
                    "metrics":   pkt.get("metrics", {}),
                    "generated": pkt.get("generated_at", ""),
                }
        if div_data:
            results[division] = div_data
    return results


def _weekly_health_summary() -> dict:
    """Summarize health trends for the week (no raw data exposed)."""
    health = load_health_log()
    entries = health.get("entries", [])
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = [e for e in entries if (e.get("date") or "") >= cutoff]

    if not recent:
        return {"logged_days": 0, "summary": "No health data this week."}

    sleep_hours = [e["sleep_hours"] for e in recent if e.get("sleep_hours") is not None]
    skipped = sum(1 for e in recent if e.get("skipped"))

    avg_sleep = round(sum(sleep_hours) / len(sleep_hours), 1) if sleep_hours else None

    return {
        "logged_days": len(recent),
        "skipped_days": skipped,
        "avg_sleep": avg_sleep,
        "summary": (
            f"{len(recent)} health logs this week"
            f"{f', avg sleep {avg_sleep}h' if avg_sleep else ''}"
            f"{f', {skipped} skipped' if skipped else ''}"
        ),
    }


def _weekly_trading_summary() -> dict:
    """Summarize trading performance for the week."""
    trade_log = load_trade_log()
    sessions = trade_log.get("sessions", trade_log.get("trades", []))
    cutoff = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    recent = [s for s in sessions if (s.get("date") or "") >= cutoff]

    if not recent:
        return {"sessions": 0, "summary": "No trading sessions this week."}

    total_trades = sum(s.get("stats", {}).get("total_trades", 0) for s in recent if s.get("stats"))
    win_rates = [s["stats"]["win_rate"] for s in recent
                 if s.get("stats") and s["stats"].get("win_rate") is not None]
    pnls = [s["stats"]["total_pnl"] for s in recent
            if s.get("stats") and s["stats"].get("total_pnl") is not None]

    avg_wr = round(sum(win_rates) / len(win_rates), 1) if win_rates else None
    total_pnl = round(sum(pnls), 2) if pnls else None

    return {
        "sessions": len(recent),
        "total_trades": total_trades,
        "avg_win_rate": avg_wr,
        "total_pnl": total_pnl,
        "summary": (
            f"{len(recent)} trading session(s), {total_trades} trades"
            f"{f', avg WR {avg_wr}%' if avg_wr else ''}"
            f"{f', PnL ${total_pnl}' if total_pnl is not None else ''}"
        ),
    }


def _count_escalations(division_data: dict) -> dict:
    """Count escalations across all divisions."""
    escalation_count = 0
    escalation_details = []
    for division, skills in division_data.items():
        for skill, data in skills.items():
            if data.get("escalate"):
                escalation_count += 1
                escalation_details.append(f"{division}/{skill}")
    return {
        "count": escalation_count,
        "details": escalation_details[:5],
    }


def run() -> dict:
    HOT_DIR.mkdir(parents=True, exist_ok=True)
    PACKET_DIR.mkdir(parents=True, exist_ok=True)

    # Gather data from all sources
    division_data = _read_division_packets()
    health_summary = _weekly_health_summary()
    trading_summary = _weekly_trading_summary()
    escalations = _count_escalations(division_data)

    # Build context for LLM
    division_summaries = []
    for division, skills in division_data.items():
        skill_lines = []
        for skill, data in skills.items():
            status_icon = "!" if data.get("escalate") else "+"
            skill_lines.append(f"  [{status_icon}] {skill}: {data.get('summary', 'no summary')[:120]}")
        division_summaries.append(f"{division.upper()}:\n" + "\n".join(skill_lines))

    context = (
        f"WEEKLY RETROSPECTIVE — Past {LOOKBACK_DAYS} days\n\n"
        f"Health: {health_summary['summary']}\n"
        f"Trading: {trading_summary['summary']}\n"
        f"Escalations: {escalations['count']} total"
        f"{f' ({', '.join(escalations['details'])})' if escalations['details'] else ''}\n\n"
        f"DIVISION STATUS:\n" + "\n".join(division_summaries)
    )

    # Try LLM synthesis
    if is_available(MODEL, host=OLLAMA_HOST):
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Personal Division's weekly retrospective analyst for J_Claw. "
                    "Given the past week's data across all divisions, write a concise Monday morning "
                    "briefing for Matthew. Structure:\n"
                    "1. One sentence on overall week assessment\n"
                    "2. Top 2-3 wins or positive patterns\n"
                    "3. Top 2-3 risks or areas needing attention\n"
                    "4. One sentence recommending this week's focus\n\n"
                    "Rules:\n"
                    "- Be specific to the data — no generic advice\n"
                    "- Do NOT include raw health values (sleep hours, medication) — "
                    "only safe summaries\n"
                    "- Keep it under 200 words\n"
                    "- Start directly — no 'Here is your briefing' preamble"
                ),
            },
            {"role": "user", "content": context},
        ]

        try:
            analysis = chat(MODEL, messages, host=OLLAMA_HOST, temperature=0.2, max_tokens=400, task_type="weekly-retrospective")
            # Strip common LLM preamble
            lines = analysis.strip().splitlines()
            if lines and lines[0].rstrip().endswith(":"):
                analysis = "\n".join(lines[1:]).lstrip()
            summary = analysis
        except Exception as e:
            log.warning("weekly-retrospective LLM failed: %s", e)
            summary = _fallback_summary(division_data, health_summary, trading_summary, escalations)
    else:
        summary = _fallback_summary(division_data, health_summary, trading_summary, escalations)

    active_divisions = len(division_data)

    packet = {
        "status":         "success",
        "escalate":       escalations["count"] > 2,
        "escalation_reason": (
            f"{escalations['count']} escalations across divisions this week"
            if escalations["count"] > 2 else ""
        ),
        "summary":        summary,
        "scanned_at":     datetime.now(timezone.utc).isoformat(),
        "metrics": {
            "active_divisions":   active_divisions,
            "escalation_count":   escalations["count"],
            "health_logged_days": health_summary.get("logged_days", 0),
            "trading_sessions":   trading_summary.get("sessions", 0),
            "trading_total_pnl":  trading_summary.get("total_pnl"),
            "avg_sleep":          health_summary.get("avg_sleep"),
        },
    }

    with open(PACKET_DIR / "weekly-retrospective.json", "w", encoding="utf-8") as f:
        json.dump(packet, f, indent=2)

    return packet


def _fallback_summary(
    division_data: dict,
    health_summary: dict,
    trading_summary: dict,
    escalations: dict,
) -> str:
    """Deterministic fallback when LLM is unavailable."""
    parts = [
        f"Weekly retrospective: {len(division_data)} division(s) active.",
        health_summary["summary"] + ".",
        trading_summary["summary"] + ".",
    ]
    if escalations["count"] > 0:
        parts.append(f"{escalations['count']} escalation(s) this week: {', '.join(escalations['details'][:3])}.")
    else:
        parts.append("No escalations this week.")
    return " ".join(parts)
