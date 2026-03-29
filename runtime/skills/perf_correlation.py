"""
perf-correlation skill — Tier 1 LLM (Qwen2.5 7B).
Cross-references health log against trading performance.
Only surfaces patterns with ≥3 data points.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path

from runtime.config import SKILL_MODELS
from runtime.ollama_client import chat, is_available
from runtime.tools.state import load_health_log, STATE_DIR
from runtime.tools.trading import load_all_time_trades

log = logging.getLogger(__name__)
MODEL = SKILL_MODELS["perf-correlation"]


def _load_trade_log() -> list:
    path = STATE_DIR / "trade-log.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8-sig") as f:
            data = json.load(f)
        return data.get("sessions", [])
    except Exception as e:
        log.error("Failed to load trade-log: %s", e)
        return []


def _last_n_days(entries: list, n: int = 14) -> list:
    cutoff = (date.today() - timedelta(days=n)).isoformat()
    return [e for e in entries if e and e.get("date", "") >= cutoff]


def run() -> dict:
    today = date.today().isoformat()

    # Load state
    health_state  = load_health_log()
    health_entries = _last_n_days(health_state.get("entries", []))

    # Prefer agent-network's rich historical trade data; fall back to Z_Claw trade-log
    agent_trades = load_all_time_trades(days=14)
    if agent_trades:
        # Convert to session-like format grouped by date for compatibility
        from collections import defaultdict
        by_date = defaultdict(list)
        for t in agent_trades:
            by_date[t["date"]].append(t)
        trade_sessions = []
        for d, trades in sorted(by_date.items()):
            wins = sum(1 for t in trades if t["result"] == "win")
            losses = sum(1 for t in trades if t["result"] == "loss")
            total = len(trades)
            pnls = [t["pnl"] for t in trades if t.get("pnl") is not None]
            rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]
            trade_sessions.append({
                "date": d,
                "stats": {
                    "total_trades": total,
                    "wins": wins,
                    "losses": losses,
                    "win_rate": round(wins / total * 100, 1) if total else None,
                    "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
                    "total_pnl": round(sum(pnls), 2) if pnls else None,
                }
            })
    else:
        trade_sessions = _last_n_days(_load_trade_log())

    today_health = next(
        (e for e in health_entries if e.get("date") == today), None
    )
    today_trades = next(
        (s for s in trade_sessions if s.get("date") == today), None
    )

    if len(health_entries) < 3:
        return {
            "patterns": [],
            "summary":  "Insufficient data — need at least 3 health log entries for correlation.",
            "data_points": len(health_entries),
            "status": "partial",
        }

    # Build data context for LLM
    health_rows = "\n".join(
        f"{e['date']}: sleep={e.get('sleep_hours')}h q={e.get('sleep_quality')} "
        f"adderall={e.get('adderall_dose')} @{e.get('adderall_time')} "
        f"exercise={e.get('exercise_duration_min','0')}min "
        f"hydration={e.get('hydration','?')}"
        for e in health_entries if not e.get("skipped")
    )

    trade_rows = "\n".join(
        f"{s['date']}: trades={s['stats'].get('total_trades',0)} "
        f"win_rate={s['stats'].get('win_rate','?')}% "
        f"avg_r={s['stats'].get('avg_r','?')} "
        f"pnl=${s['stats'].get('total_pnl','?')}"
        for s in trade_sessions if s.get("stats")
    )

    if not is_available(MODEL):
        return {
            "patterns": [],
            "summary":  "Model unavailable — correlation not run.",
            "data_points": len(health_entries),
            "status": "partial",
        }

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Personal Division orchestrator for J_Claw. "
                "Analyze the correlation between Tyler's health data and trading performance. "
                "\n\nRules:"
                "\n- Only report patterns that appear in ≥3 data points"
                "\n- Be specific to Matthew's data — no generic health advice"
                "\n- If no meaningful pattern exists, say so explicitly"
                "\n- Max 3 patterns, 1 sentence each"
                "\n- Format: 'Pattern: [finding] (N data points)'"
                "\n- If insufficient trading data, note it"
            ),
        },
        {
            "role": "user",
            "content": (
                f"Health data (last 14 days):\n{health_rows or 'none'}\n\n"
                f"Trading data (last 14 days):\n{trade_rows or 'none'}"
            ),
        },
    ]

    analysis = chat(MODEL, messages, temperature=0.15, max_tokens=300, task_type="perf-correlation")

    return {
        "patterns":    [analysis],
        "summary":     analysis,
        "data_points": len(health_entries),
        "status":      "success",
    }
