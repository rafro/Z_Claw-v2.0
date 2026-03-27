"""
backtester skill — reads agent-network cycle state, evaluates strategy quality,
detects strategy changes, and returns a structured result for the trading orchestrator.
Pure Python — no LLM required.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from runtime.tools.trading import load_cycle_state, load_active_strategy

log = logging.getLogger(__name__)

PACKET_PATH = Path("divisions/trading/packets/backtester.json")
OOS_MIN_TRADES = 50


def run() -> dict:
    """
    Returns result dict:
    {
        "status": "success | partial | failed",
        "summary": str,
        "metrics": {...},
        "quality_flags": {...},
        "escalate": bool,
        "escalation_reason": str,
        "strategy_changed": bool | None,
    }
    """

    # ── Step 1: Load cycle state ───────────────────────────────────────────────
    try:
        cycle = load_cycle_state()
    except Exception as e:
        log.error("Failed to load cycle state: %s", e)
        return _error_result(f"Failed to load cycle state: {e}")

    if not cycle:
        return {
            "status": "partial",
            "summary": "agent-network cycle state not found — backtester did not run",
            "metrics": {},
            "quality_flags": {},
            "escalate": False,
            "escalation_reason": "",
            "strategy_changed": None,
        }

    strat = cycle.get("active_strategy")
    if not strat:
        return {
            "status": "partial",
            "summary": "cycle state present but no active strategy found",
            "metrics": {"cycle_number": cycle.get("cycle_number")},
            "quality_flags": {},
            "escalate": False,
            "escalation_reason": "",
            "strategy_changed": None,
        }

    cycle_num    = cycle.get("cycle_number", 0)
    strategy_id  = strat.get("strategy_id", "")
    strategy_name = strat.get("strategy_name", "")

    # ── Step 2: Detect strategy change ────────────────────────────────────────
    prev_strategy_id   = None
    prev_strategy_name = None
    strategy_changed   = None

    if PACKET_PATH.exists():
        try:
            with open(PACKET_PATH, encoding="utf-8") as f:
                prev_pkt = json.load(f)
            prev_metrics = prev_pkt.get("metrics", {})
            prev_strategy_id   = prev_metrics.get("strategy_id")
            prev_strategy_name = prev_metrics.get("strategy_name")
            if prev_strategy_id:
                strategy_changed = (strategy_id != prev_strategy_id)
        except Exception as e:
            log.warning("Could not load previous backtester packet for comparison: %s", e)
            strategy_changed = None
    else:
        strategy_changed = None  # first run

    # ── Step 3: Extract metrics ────────────────────────────────────────────────
    sharpe         = strat.get("sharpe")
    sortino        = strat.get("sortino")
    win_rate       = strat.get("win_rate")
    profit_factor  = strat.get("profit_factor")
    max_drawdown   = strat.get("max_drawdown")
    avg_r          = strat.get("avg_r")
    avg_win_r      = strat.get("avg_win_r")
    avg_loss_r     = strat.get("avg_loss_r")
    rr_ratio       = strat.get("rr_ratio")
    theoretical_ev = strat.get("theoretical_ev_r")
    empirical_ev   = strat.get("empirical_ev_r")
    ev_drift       = strat.get("ev_drift_r")
    total_pnl_usd  = strat.get("total_pnl_usd")
    slippage_adjusted_pnl = round(total_pnl_usd * (1 - 0.001), 2) if total_pnl_usd is not None else None
    annualised_ret = strat.get("annualised_return_pct")
    oos_sharpe     = strat.get("oos_sharpe")
    oos_win_rate   = strat.get("oos_win_rate")
    oos_trade_count = strat.get("oos_trade_count")
    oos_penalty    = strat.get("oos_penalty")
    confidence     = strat.get("confidence_rating", "")
    score          = strat.get("score")
    score_detail   = strat.get("score_detail", {})
    direction      = strat.get("direction", "")
    mc_p95_dd      = strat.get("mc_p95_dd")
    mc_risk_class  = strat.get("mc_risk_class")

    # ── Step 4: Quality flags ──────────────────────────────────────────────────
    oos_weak      = bool(
        (oos_sharpe is not None and oos_sharpe < 0.3) or
        (oos_win_rate is not None and oos_win_rate < 0.35) or
        (oos_trade_count is not None and oos_trade_count < OOS_MIN_TRADES)
    )
    high_ev_drift = bool(
        ev_drift is not None and abs(ev_drift) > 0.1
    )
    low_confidence = confidence.lower() in ("low", "very low") if confidence else False
    poor_drawdown  = bool(max_drawdown is not None and max_drawdown > 0.05)
    healthy        = not (oos_weak or high_ev_drift or low_confidence or poor_drawdown)

    quality_flags = {
        "oos_weak":              oos_weak,
        "high_ev_drift":         high_ev_drift,
        "low_confidence":        low_confidence,
        "poor_drawdown":         poor_drawdown,
        "healthy":               healthy,
        "walk_forward_performed": False,  # agent-network uses single 80/20 split, not rolling WF
    }

    # ── Step 5: Escalation ────────────────────────────────────────────────────
    escalate = False
    escalation_reasons = []

    if strategy_changed:
        escalate = True
        escalation_reasons.append(
            f"Strategy changed: {prev_strategy_name} → {strategy_name} (cycle {cycle_num})"
        )
    if oos_weak:
        escalate = True
        escalation_reasons.append(
            f"OOS validation weak: Sharpe {oos_sharpe}, Win Rate {oos_win_rate}"
        )
        if oos_trade_count is not None and oos_trade_count < OOS_MIN_TRADES:
            escalation_reasons.append(
                f"Insufficient OOS trades: {oos_trade_count} (minimum {OOS_MIN_TRADES} required)"
            )
    if high_ev_drift:
        escalate = True
        escalation_reasons.append(
            f"High EV drift: theoretical {theoretical_ev} vs empirical {empirical_ev}"
        )
    if low_confidence and strategy_changed:
        # Already captured by strategy_changed escalation above — add detail
        escalation_reasons.append(f"New strategy has low confidence rating: {confidence}")

    escalation_reason = " | ".join(escalation_reasons)

    # ── Step 6: Summary ────────────────────────────────────────────────────────
    win_rate_pct = round((win_rate or 0) * 100, 1) if win_rate is not None else "N/A"
    prefix = "[NEW STRATEGY] " if strategy_changed else ""
    summary = (
        f"{prefix}Cycle {cycle_num} | Strategy: {strategy_name} | "
        f"Sharpe: {sharpe} | Win Rate: {win_rate_pct}% | "
        f"OOS Sharpe: {oos_sharpe} | Confidence: {confidence}"
    )

    metrics = {
        "cycle_number":          cycle_num,
        "strategy_id":           strategy_id,
        "strategy_name":         strategy_name,
        "strategy_changed":      strategy_changed,
        "prev_strategy_name":    prev_strategy_name,
        "direction":             direction,
        "sharpe":                sharpe,
        "sortino":               sortino,
        "win_rate":              win_rate,
        "win_rate_pct":          win_rate_pct,
        "profit_factor":         profit_factor,
        "max_drawdown":          max_drawdown,
        "avg_r":                 avg_r,
        "avg_win_r":             avg_win_r,
        "avg_loss_r":            avg_loss_r,
        "rr_ratio":              rr_ratio,
        "theoretical_ev_r":      theoretical_ev,
        "empirical_ev_r":        empirical_ev,
        "ev_drift_r":            ev_drift,
        "total_pnl_usd":         total_pnl_usd,
        "slippage_adjusted_pnl": slippage_adjusted_pnl,
        "annualised_return_pct": annualised_ret,
        "oos_sharpe":            oos_sharpe,
        "oos_win_rate":          oos_win_rate,
        "oos_trade_count":       oos_trade_count,
        "oos_penalty":           oos_penalty,
        "confidence_rating":     confidence,
        "score":                 score,
        "mc_p95_dd":             mc_p95_dd,
        "mc_risk_class":         mc_risk_class,
        "quality_flags":         quality_flags,
    }

    log.info(
        "Backtester complete: cycle=%d strat=%s sharpe=%.3f changed=%s escalate=%s",
        cycle_num, strategy_id, sharpe or 0, strategy_changed, escalate,
    )

    return {
        "status":             "success",
        "summary":            summary,
        "metrics":            metrics,
        "quality_flags":      quality_flags,
        "escalate":           escalate,
        "escalation_reason":  escalation_reason,
        "strategy_changed":   strategy_changed,
    }


def _error_result(msg: str) -> dict:
    return {
        "status":            "failed",
        "summary":           msg,
        "metrics":           {},
        "quality_flags":     {},
        "escalate":          True,
        "escalation_reason": msg,
        "strategy_changed":  None,
    }
