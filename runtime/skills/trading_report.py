"""
trading-report skill — Tier 0 data tool + Tier 1 LLM interpretation.
Loads Alpaca state, calculates stats (pure Python), then asks the
orchestrator LLM to interpret patterns.
"""

import logging

from runtime.config import SKILL_MODELS
from runtime.tools.trading import (
    load_today_trades, pair_trades, calc_session_stats,
    save_session, append_to_trade_log
)
from runtime.ollama_client import chat, is_available

log = logging.getLogger(__name__)
MODEL = SKILL_MODELS["trading-report"]


def run() -> dict:
    """
    Returns result dict for the trading orchestrator:
    {
        "paired": [...],
        "stats": {...},
        "source": str,
        "interpretation": str,
        "escalate": bool,
        "escalation_reason": str,
        "status": str,
    }
    """
    # ── Step 1: Load and pair trades (pure Python) ────────────────────────────
    raw_trades, source = load_today_trades()

    if source == "none":
        return {
            "paired": [], "stats": {}, "source": "none",
            "interpretation": "Trading system not yet activated.",
            "escalate": False, "escalation_reason": "",
            "status": "partial",
        }

    paired = pair_trades(raw_trades)

    if not paired:
        return {
            "paired": [], "stats": {"total_trades": 0}, "source": source,
            "interpretation": "No closed trades today.",
            "escalate": False, "escalation_reason": "",
            "status": "success",
        }

    # ── Step 2: Calculate stats (pure Python) ─────────────────────────────────
    stats = calc_session_stats(paired)

    # ── Step 3: Persist ───────────────────────────────────────────────────────
    save_session(paired, stats, source)
    append_to_trade_log(paired, stats, source)

    # ── Step 4: Detect escalation triggers (pure logic) ───────────────────────
    escalate = False
    escalation_reason = ""

    win_rate = stats.get("win_rate")
    total    = stats.get("total_trades", 0)
    worst_r  = stats.get("worst_r")

    if win_rate is not None and win_rate < 40 and total >= 5:
        escalate = True
        escalation_reason = f"Win rate {win_rate}% on {total} trades — below threshold."
    if worst_r is not None and worst_r < -2.0:
        escalate = True
        escalation_reason += f" Single loss of {worst_r}R exceeds max risk."

    # ── Step 5: LLM interpretation ────────────────────────────────────────────
    interpretation = _interpret(stats, paired, source)

    return {
        "paired":              paired,
        "stats":               stats,
        "source":              source,
        "interpretation":      interpretation,
        "escalate":            escalate,
        "escalation_reason":   escalation_reason.strip(),
        "status":              "success",
    }


def _interpret(stats: dict, trades: list, source: str) -> str:
    if not is_available(MODEL):
        s = stats
        return (
            f"Trades: {s.get('total_trades',0)} | "
            f"W/L: {s.get('wins',0)}/{s.get('losses',0)} | "
            f"Win Rate: {s.get('win_rate','N/A')}% | "
            f"Avg R: {s.get('avg_r','N/A')} | "
            f"PnL: ${s.get('total_pnl','N/A')}"
        )

    trade_lines = "\n".join(
        f"- {t['symbol']} {t['side']} | R={t['r_multiple']} | PnL=${t['pnl']} | {t['result']}"
        for t in trades[:10]
    )

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Trading Division orchestrator for J_Claw. "
                "Analyze today's trading session and write a 1–3 sentence interpretation "
                "for the executive briefing. Be specific: mention win rate, R multiples, "
                "any patterns across trades, risk discipline, or concerns. "
                "Do not pad. No generic advice."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Source: {source}\n"
                f"Stats: total={stats.get('total_trades')} wins={stats.get('wins')} "
                f"losses={stats.get('losses')} win_rate={stats.get('win_rate')}% "
                f"avg_r={stats.get('avg_r')} best_r={stats.get('best_r')} "
                f"worst_r={stats.get('worst_r')} pnl=${stats.get('total_pnl')}\n"
                f"Trades:\n{trade_lines}"
            ),
        },
    ]
    return chat(MODEL, messages, temperature=0.2, max_tokens=200)
