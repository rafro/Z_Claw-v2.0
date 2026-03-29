"""
backtester skill — reads agent-network cycle state, evaluates strategy quality,
detects strategy changes, and returns a structured result for the trading orchestrator.
Pure Python — no LLM required.
"""

import json
import logging
import math
import random
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

    # ── Step 7: Deep analysis (runs on strategy change or trouble) ──────────
    deep_analysis = {}
    run_deep = strategy_changed or not healthy

    if run_deep:
        # Load trade log from virtual account for deep analysis
        trade_log = []
        try:
            va_path = Path("C:/Users/Tyler/agent-network/state/virtual_account.json")
            if va_path.exists():
                with open(va_path, encoding="utf-8-sig") as f:
                    va_data = json.load(f)
                trade_log = va_data.get("trade_log", [])
        except Exception as e:
            log.warning("Could not load trade log for deep analysis: %s", e)

        if trade_log:
            wf = _walk_forward_analysis(trade_log)
            mc = _monte_carlo_analysis(trade_log)
            ext = _calc_extended_metrics(trade_log)
            health = _strategy_health_score(metrics, quality_flags, wf, mc)

            deep_analysis = {
                "walk_forward": wf,
                "monte_carlo": mc,
                "extended_metrics": ext,
                "health_score": health,
            }

            # Update quality flags with walk-forward status
            quality_flags["walk_forward_performed"] = wf.get("performed", False)

            # Add deep-analysis-driven escalation
            if health["rating"] == "critical":
                escalate = True
                escalation_reasons.append(
                    f"Strategy health CRITICAL (score {health['score']}/100)"
                )
                escalation_reason = " | ".join(escalation_reasons)
            elif health["rating"] == "weak" and strategy_changed:
                escalate = True
                escalation_reasons.append(
                    f"New strategy health WEAK (score {health['score']}/100)"
                )
                escalation_reason = " | ".join(escalation_reasons)

            if mc.get("performed") and mc.get("risk_class") == "high":
                escalate = True
                escalation_reasons.append(
                    f"Monte Carlo risk class HIGH (p95 DD={mc.get('max_drawdown_p95', 'N/A')})"
                )
                escalation_reason = " | ".join(escalation_reasons)

            if wf.get("performed") and wf.get("stability") == "unstable":
                escalate = True
                escalation_reasons.append(
                    f"Walk-forward analysis UNSTABLE (Sharpe std={wf.get('sharpe_std', 'N/A')})"
                )
                escalation_reason = " | ".join(escalation_reasons)

            log.info("Deep analysis complete: health=%s wf=%s mc_risk=%s",
                     health.get("rating"), wf.get("stability"), mc.get("risk_class"))
        else:
            log.info("Deep analysis skipped: no trade log available")

    metrics["deep_analysis"] = deep_analysis if deep_analysis else None

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


# ── Deep analysis functions (walk-forward, Monte Carlo, extended metrics) ──────

def _walk_forward_analysis(trade_log: list, n_folds: int = 5) -> dict:
    """
    Rolling walk-forward analysis over the trade log.
    Splits trades into n_folds sequential windows, computes per-fold Sharpe
    and win rate, and checks for consistency across folds.
    Returns dict with fold results and overall stability rating.
    """
    if not trade_log or len(trade_log) < n_folds * 5:
        return {"performed": False, "reason": "insufficient trades for walk-forward"}

    completed = [t for t in trade_log if t.get("pnl") is not None]
    if len(completed) < n_folds * 5:
        return {"performed": False, "reason": "insufficient completed trades"}

    fold_size = len(completed) // n_folds
    folds = []
    for i in range(n_folds):
        start = i * fold_size
        end = start + fold_size if i < n_folds - 1 else len(completed)
        fold_trades = completed[start:end]

        pnls = [t.get("pnl", 0) for t in fold_trades]
        wins = sum(1 for p in pnls if p > 0)
        total = len(pnls)
        win_rate = wins / total if total > 0 else 0

        mean_pnl = sum(pnls) / len(pnls) if pnls else 0
        if len(pnls) > 1:
            variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
            std_pnl = math.sqrt(variance) if variance > 0 else 0.001
        else:
            std_pnl = 0.001
        fold_sharpe = mean_pnl / std_pnl if std_pnl > 0 else 0

        folds.append({
            "fold": i + 1,
            "trades": total,
            "win_rate": round(win_rate, 3),
            "sharpe": round(fold_sharpe, 3),
            "total_pnl": round(sum(pnls), 2),
        })

    sharpes = [f["sharpe"] for f in folds]
    win_rates = [f["win_rate"] for f in folds]
    avg_sharpe = sum(sharpes) / len(sharpes)
    sharpe_std = math.sqrt(sum((s - avg_sharpe) ** 2 for s in sharpes) / len(sharpes)) if len(sharpes) > 1 else 0

    # Stability: low variance in Sharpe across folds is good
    profitable_folds = sum(1 for f in folds if f["total_pnl"] > 0)
    if sharpe_std < 0.5 and profitable_folds >= n_folds * 0.6:
        stability = "stable"
    elif sharpe_std < 1.0 and profitable_folds >= n_folds * 0.4:
        stability = "moderate"
    else:
        stability = "unstable"

    return {
        "performed": True,
        "n_folds": n_folds,
        "folds": folds,
        "avg_sharpe": round(avg_sharpe, 3),
        "sharpe_std": round(sharpe_std, 3),
        "profitable_folds": profitable_folds,
        "stability": stability,
    }


def _monte_carlo_analysis(trade_log: list, n_simulations: int = 1000,
                          confidence: float = 0.95) -> dict:
    """
    Monte Carlo simulation: randomly reshuffle trade PnLs to estimate
    distribution of max drawdown and total return.
    Returns p5/p50/p95 for drawdown and total return.
    """
    completed = [t for t in trade_log if t.get("pnl") is not None]
    pnls = [t.get("pnl", 0) for t in completed]

    if len(pnls) < 10:
        return {"performed": False, "reason": "insufficient trades for Monte Carlo"}

    max_dds = []
    total_returns = []

    for _ in range(n_simulations):
        shuffled = pnls[:]
        random.shuffle(shuffled)

        # Simulate equity curve
        equity = 10000.0  # baseline
        peak = equity
        max_dd = 0.0
        for pnl in shuffled:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
        max_dds.append(max_dd)
        total_returns.append((equity - 10000.0) / 10000.0)

    max_dds.sort()
    total_returns.sort()

    def percentile(data: list, pct: float) -> float:
        idx = int(len(data) * pct)
        idx = min(idx, len(data) - 1)
        return round(data[idx], 4)

    p5_idx = int(n_simulations * 0.05)
    p50_idx = int(n_simulations * 0.50)
    p95_idx = int(n_simulations * 0.95)

    mc_p95_dd = max_dds[min(p95_idx, len(max_dds) - 1)]

    # Risk classification
    if mc_p95_dd < 0.05:
        risk_class = "low"
    elif mc_p95_dd < 0.10:
        risk_class = "moderate"
    elif mc_p95_dd < 0.20:
        risk_class = "elevated"
    else:
        risk_class = "high"

    return {
        "performed": True,
        "n_simulations": n_simulations,
        "max_drawdown_p5": percentile(max_dds, 0.05),
        "max_drawdown_p50": percentile(max_dds, 0.50),
        "max_drawdown_p95": round(mc_p95_dd, 4),
        "total_return_p5": percentile(total_returns, 0.05),
        "total_return_p50": percentile(total_returns, 0.50),
        "total_return_p95": percentile(total_returns, 0.95),
        "risk_class": risk_class,
    }


def _calc_extended_metrics(trade_log: list) -> dict:
    """
    Calculate extended performance metrics beyond basic Sharpe/win-rate.
    Includes: Calmar ratio, expectancy per trade, payoff ratio,
    max consecutive wins/losses, recovery factor.
    """
    completed = [t for t in trade_log if t.get("pnl") is not None]
    pnls = [t.get("pnl", 0) for t in completed]

    if not pnls:
        return {}

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl = sum(pnls)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.001
    payoff_ratio = round(avg_win / avg_loss, 3) if avg_loss > 0 else 0
    expectancy = round(total_pnl / len(pnls), 2) if pnls else 0

    # Max consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    current_wins = 0
    current_losses = 0
    for p in pnls:
        if p > 0:
            current_wins += 1
            current_losses = 0
            max_consec_wins = max(max_consec_wins, current_wins)
        else:
            current_losses += 1
            current_wins = 0
            max_consec_losses = max(max_consec_losses, current_losses)

    # Max drawdown from equity curve
    equity = 10000.0
    peak = equity
    max_dd = 0.0
    max_dd_duration = 0
    current_dd_duration = 0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
            current_dd_duration = 0
        else:
            current_dd_duration += 1
            max_dd_duration = max(max_dd_duration, current_dd_duration)
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Calmar = annualized return / max drawdown
    # Approximate: assume 252 trading days
    if len(pnls) > 0 and max_dd > 0:
        avg_daily = total_pnl / len(pnls)
        annualized = avg_daily * 252
        calmar = round(annualized / (max_dd * 10000), 3)
    else:
        calmar = 0

    # Recovery factor = total PnL / max drawdown (in $)
    max_dd_usd = max_dd * 10000  # based on $10k baseline
    recovery_factor = round(total_pnl / max_dd_usd, 3) if max_dd_usd > 0 else 0

    return {
        "expectancy_per_trade": expectancy,
        "payoff_ratio": payoff_ratio,
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "max_drawdown_duration": max_dd_duration,
        "calmar_ratio": calmar,
        "recovery_factor": recovery_factor,
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
    }


def _strategy_health_score(metrics: dict, quality_flags: dict,
                           walk_forward: dict, monte_carlo: dict) -> dict:
    """
    Compute an overall strategy health score (0-100) combining all analysis.
    Returns score and component breakdown.
    """
    components = {}
    total_weight = 0
    weighted_sum = 0

    # Sharpe contribution (weight: 25)
    sharpe = metrics.get("sharpe")
    if sharpe is not None:
        # Sharpe 0 = 0 pts, 1.0 = 50 pts, 2.0+ = 100 pts
        sharpe_score = min(100, max(0, sharpe * 50))
        components["sharpe"] = round(sharpe_score, 1)
        weighted_sum += sharpe_score * 25
        total_weight += 25

    # Win rate contribution (weight: 15)
    win_rate = metrics.get("win_rate")
    if win_rate is not None:
        # 30% = 0, 50% = 50, 70%+ = 100
        wr_score = min(100, max(0, (win_rate - 0.3) / 0.4 * 100))
        components["win_rate"] = round(wr_score, 1)
        weighted_sum += wr_score * 15
        total_weight += 15

    # Drawdown contribution (weight: 20)
    max_dd = metrics.get("max_drawdown")
    if max_dd is not None:
        # 0% dd = 100, 5% = 50, 10%+ = 0
        dd_score = min(100, max(0, (0.10 - max_dd) / 0.10 * 100))
        components["drawdown"] = round(dd_score, 1)
        weighted_sum += dd_score * 20
        total_weight += 20

    # Walk-forward stability (weight: 20)
    if walk_forward.get("performed"):
        stability = walk_forward.get("stability", "unstable")
        if stability == "stable":
            wf_score = 90
        elif stability == "moderate":
            wf_score = 55
        else:
            wf_score = 20
        components["walk_forward"] = wf_score
        weighted_sum += wf_score * 20
        total_weight += 20

    # Monte Carlo risk (weight: 20)
    if monte_carlo.get("performed"):
        risk_class = monte_carlo.get("risk_class", "high")
        if risk_class == "low":
            mc_score = 95
        elif risk_class == "moderate":
            mc_score = 70
        elif risk_class == "elevated":
            mc_score = 40
        else:
            mc_score = 15
        components["monte_carlo"] = mc_score
        weighted_sum += mc_score * 20
        total_weight += 20

    # Quality flag penalties
    penalty = 0
    if quality_flags.get("oos_weak"):
        penalty += 15
    if quality_flags.get("high_ev_drift"):
        penalty += 10
    if quality_flags.get("low_confidence"):
        penalty += 10
    components["penalty"] = penalty

    overall = (weighted_sum / total_weight) - penalty if total_weight > 0 else 0
    overall = max(0, min(100, overall))

    # Rating
    if overall >= 75:
        rating = "strong"
    elif overall >= 55:
        rating = "acceptable"
    elif overall >= 35:
        rating = "weak"
    else:
        rating = "critical"

    return {
        "score": round(overall, 1),
        "rating": rating,
        "components": components,
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
