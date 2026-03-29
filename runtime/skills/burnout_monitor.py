"""
burnout-monitor skill — Tier 0 (pure Python thresholds) + Tier 1 LLM interpretation.
Analyzes sleep trends, skipped health logs, and emotional state from trade logs.
Escalates if overload indicators exceed thresholds.
"""

import logging
from datetime import datetime, timezone, timedelta
from statistics import mean

from runtime.config import SKILL_MODELS
from runtime.tools.state import load_health_log, load_trade_log
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = SKILL_MODELS["burnout-monitor"]

LOOKBACK_DAYS   = 7
SLEEP_WARNING   = 6.5   # avg hours below this → warning
SLEEP_ALERT     = 5.5   # avg hours below this → alert
SKIP_WARNING    = 3     # skipped health logs → warning
SKIP_ALERT      = 5     # skipped health logs → alert
NEGATIVE_EMOTIONS = {"stressed", "anxious", "frustrated", "overwhelmed", "exhausted", "bad", "poor"}


def _recent_entries(entries: list, days: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for e in entries:
        try:
            ts = datetime.fromisoformat(e.get("logged_at", "").replace("Z", "+00:00"))
            if ts > cutoff:
                result.append(e)
        except Exception:
            pass
    return result


def _recent_trades(trades: list, days: int) -> list:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = []
    for t in trades:
        try:
            ts_str = t.get("timestamp") or t.get("date") or t.get("logged_at") or ""
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts > cutoff:
                result.append(t)
        except Exception:
            pass
    return result


def run() -> dict:
    health_state  = load_health_log()
    trade_state   = load_trade_log()
    health_entries = _recent_entries(health_state.get("entries", []), LOOKBACK_DAYS)
    trade_entries  = _recent_trades(trade_state.get("trades", []), LOOKBACK_DAYS)

    triggers = []
    level    = "ok"

    # ── Sleep analysis ─────────────────────────────────────────────────────────
    sleep_values = [e["sleep_hours"] for e in health_entries if e.get("sleep_hours") is not None]
    skipped      = sum(1 for e in health_entries if e.get("skipped"))
    avg_sleep    = mean(sleep_values) if sleep_values else None

    if avg_sleep is not None:
        if avg_sleep < SLEEP_ALERT:
            triggers.append(f"Critical: avg sleep {avg_sleep:.1f}h over last {LOOKBACK_DAYS}d")
            level = "alert"
        elif avg_sleep < SLEEP_WARNING:
            triggers.append(f"Low avg sleep: {avg_sleep:.1f}h over last {LOOKBACK_DAYS}d")
            if level == "ok":
                level = "warning"

    if skipped >= SKIP_ALERT:
        triggers.append(f"Critical: {skipped} health logs skipped in last {LOOKBACK_DAYS}d")
        level = "alert"
    elif skipped >= SKIP_WARNING:
        triggers.append(f"{skipped} health logs skipped in last {LOOKBACK_DAYS}d")
        if level == "ok":
            level = "warning"

    # ── Emotional state from trade logs ────────────────────────────────────────
    negative_count = sum(
        1 for t in trade_entries
        if str(t.get("emotional_state", "")).lower().strip() in NEGATIVE_EMOTIONS
    )
    if negative_count >= 5:
        triggers.append(f"Critical: {negative_count} negative emotional states in recent trades")
        level = "alert"
    elif negative_count >= 3:
        triggers.append(f"{negative_count} negative emotional states in recent trades")
        if level == "ok":
            level = "warning"

    # ── LLM interpretation if issues detected ─────────────────────────────────
    if triggers and is_available(MODEL):
        sleep_str = f"avg {avg_sleep:.1f}h" if avg_sleep is not None else "insufficient data"
        context = (
            f"Burnout check — last {LOOKBACK_DAYS} days:\n"
            f"Sleep: {sleep_str}\n"
            f"Skipped health logs: {skipped}\n"
            f"Negative trading emotions: {negative_count}\n"
            f"Triggers: {'; '.join(triggers)}"
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Personal Division burnout monitor for J_Claw. "
                    "Given these signals, write one direct sentence for Matthew "
                    "about his current state and what to watch. "
                    "Be honest, not alarmist. No padding."
                ),
            },
            {"role": "user", "content": context},
        ]
        try:
            recommendation = chat(MODEL, messages, temperature=0.2, max_tokens=100, task_type="burnout-monitor")
        except Exception as e:
            log.warning("burnout-monitor LLM failed: %s", e)
            recommendation = triggers[0]
    else:
        recommendation = triggers[0] if triggers else "All indicators within normal range."

    return {
        "status":         level,
        "level":          level,
        "triggers":       triggers,
        "recommendation": recommendation,
        "escalate":       level == "alert",
        "escalation_reason": "; ".join(triggers) if level == "alert" else "",
        "metrics": {
            "avg_sleep_hours":    round(avg_sleep, 2) if avg_sleep is not None else None,
            "skipped_logs":       skipped,
            "negative_emotions":  negative_count,
            "health_entries":     len(health_entries),
            "trade_entries":      len(trade_entries),
        },
        "model_available": is_available(MODEL),
    }
