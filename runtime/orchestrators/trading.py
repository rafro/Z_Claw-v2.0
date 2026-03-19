"""
Trading Division Orchestrator.
Runs trading-report, interprets results, compiles executive packet.
"""

import logging

from runtime.skills import trading_report
from runtime import packet
from runtime.tools.xp import grant_skill_xp

log = logging.getLogger(__name__)


def run_trading_report() -> dict:
    log.info("=== Trading Division: trading-report run ===")

    result = trading_report.run()
    stats  = result.get("stats", {})

    # Build summary line
    if result["source"] == "none":
        summary = "Trading system not yet activated — no Alpaca state files found."
        status  = "partial"
    elif stats.get("total_trades", 0) == 0:
        summary = "No closed trades today."
        status  = "success"
    else:
        summary = (
            f"Trades: {stats['total_trades']} | "
            f"W/L: {stats.get('wins',0)}/{stats.get('losses',0)} | "
            f"Win Rate: {stats.get('win_rate','?')}% | "
            f"Avg R: {stats.get('avg_r','?')} | "
            f"PnL: ${stats.get('total_pnl','?')}"
        )
        if result.get("interpretation"):
            summary += f"\n{result['interpretation']}"
        status = result["status"]

    pkt = packet.build(
        division="trading",
        skill="trading-report",
        status=status,
        summary=summary,
        metrics={
            "total_trades": stats.get("total_trades", 0),
            "wins":         stats.get("wins", 0),
            "losses":       stats.get("losses", 0),
            "win_rate":     stats.get("win_rate"),
            "avg_r":        stats.get("avg_r"),
            "best_r":       stats.get("best_r"),
            "worst_r":      stats.get("worst_r"),
            "total_pnl":    stats.get("total_pnl"),
            "source":       result.get("source", "none"),
        },
        artifact_refs=[{"bundle_id": "trade-session-today", "location": "hot"}],
        escalate=result.get("escalate", False),
        escalation_reason=result.get("escalation_reason", ""),
    )

    packet.write(pkt)
    grant_skill_xp("trading-report")
    log.info("Trading packet written. Status=%s", status)
    return pkt
