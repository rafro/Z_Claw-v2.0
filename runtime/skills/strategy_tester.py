"""
strategy_tester skill — self-contained backtest engine that tests strategy schemas
against historical data using Z_Claw's own indicators.
Tier 0: pure Python, no LLM, no agent-network dependency.

Walks historical OHLCV bars chronologically, simulates trades via get_strategy_signals(),
computes in-sample / out-of-sample metrics, Monte Carlo risk, and scores each strategy.
"""

import logging
import math
import random
import statistics
from typing import Optional

from runtime.tools.virtual_account import (
    INSTRUMENTS,
    STOP_PCT,
    _calc_atr,
    _stop_hit,
    fetch_ohlcv,
    get_strategy_signals,
)

log = logging.getLogger(__name__)

# Period overrides for backtesting (more data than live trading needs)
_BACKTEST_PERIODS = {
    "1d": "1y",
    "4h": "60d",
    "1h": "30d",
    "15m": "5d",
}

# Slippage applied to simulated fills (same as virtual_account)
SLIPPAGE_BPS = 5


# ── Data fetching ─────────────────────────────────────────────────────────────

def _fetch_backtest_ohlcv(ticker: str, timeframe: str) -> Optional[dict]:
    """
    Fetch OHLCV with extended history for backtesting.
    Uses longer yfinance periods than the live fetch_ohlcv.
    Falls back to standard fetch_ohlcv if custom fetch fails.
    """
    period = _BACKTEST_PERIODS.get(timeframe, "1y")
    # For 4h we fetch 1h and resample, matching virtual_account logic
    yf_interval = "1h" if timeframe == "4h" else timeframe

    try:
        import yfinance as yf
        df = yf.download(ticker, period=period, interval=yf_interval,
                         auto_adjust=False, progress=False)
        if df.empty:
            log.warning("Backtest fetch empty for %s — falling back to standard", ticker)
            return fetch_ohlcv(ticker, timeframe)
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        ohlcv = {
            "ticker": ticker,
            "date":   [str(d) for d in df.index],
            "open":   [float(v) for v in df["Open"].tolist()],
            "high":   [float(v) for v in df["High"].tolist()],
            "low":    [float(v) for v in df["Low"].tolist()],
            "close":  [float(v) for v in df["Close"].tolist()],
            "volume": [float(v) for v in df["Volume"].tolist()],
        }
        # Resample 1h -> 4h if needed (same logic as virtual_account._resample_4h)
        if timeframe == "4h":
            from runtime.tools.virtual_account import _resample_4h
            ohlcv = _resample_4h(ohlcv)
        log.debug("Backtest fetched %d %s bars for %s", len(ohlcv["close"]), timeframe, ticker)
        return ohlcv
    except ImportError:
        log.error("yfinance not installed — run: pip install yfinance pandas")
        return None
    except Exception as e:
        log.warning("Backtest fetch failed for %s (%s) — falling back", ticker, e)
        return fetch_ohlcv(ticker, timeframe)


# ── Trade simulation ──────────────────────────────────────────────────────────

def _simulate_trades(strategy: dict, ohlcv: dict) -> list[dict]:
    """
    Walk bars chronologically, simulate entries/exits using get_strategy_signals.
    No lookahead — only data up to the current bar is passed.
    Returns list of completed trade dicts.
    """
    close = ohlcv["close"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    volume = ohlcv.get("volume", [0] * len(close))
    n = len(close)

    strategy_id = strategy.get("id") or strategy.get("strategy_id") or strategy.get("strategy_name", "ema_crossover")

    trades = []
    position = None  # None or dict with entry info

    for bar_idx in range(40, n):
        # Build partial OHLCV up to this bar (no lookahead)
        partial_ohlcv = {
            "close":  close[:bar_idx + 1],
            "high":   high[:bar_idx + 1],
            "low":    low[:bar_idx + 1],
            "volume": volume[:bar_idx + 1],
        }

        current_price = close[bar_idx]
        if current_price <= 0:
            continue

        signal = get_strategy_signals(strategy_id, partial_ohlcv)

        # ── Check exit for open position ──────────────────────────────────
        if position is not None:
            stop_price = position["stop_loss"]
            bar_low = low[bar_idx]
            bar_high = high[bar_idx]
            side = position["side"]

            # Check stop hit using bar's low/high (more realistic than close only)
            stop_triggered = False
            if side == "buy" and bar_low <= stop_price:
                stop_triggered = True
                exit_price = stop_price  # filled at stop level
            elif side == "sell" and bar_high >= stop_price:
                stop_triggered = True
                exit_price = stop_price

            if stop_triggered:
                # Apply slippage on exit
                if side == "buy":
                    fill_price = exit_price * (1 - SLIPPAGE_BPS / 10000)
                else:
                    fill_price = exit_price * (1 + SLIPPAGE_BPS / 10000)
                pnl_pct = _compute_pnl_pct(position["entry_price"], fill_price, side)
                r_mult = pnl_pct / (STOP_PCT * 100) if STOP_PCT > 0 else 0.0
                trades.append({
                    "entry_bar": position["entry_bar"],
                    "exit_bar": bar_idx,
                    "entry_price": position["entry_price"],
                    "exit_price": fill_price,
                    "side": side,
                    "pnl_pct": pnl_pct,
                    "r_multiple": round(r_mult, 4),
                    "bars_held": bar_idx - position["entry_bar"],
                    "exit_reason": "stop_loss",
                })
                position = None
                continue  # don't open a new trade on the same bar we exited

            elif signal["exit"]:
                # Signal-based exit
                if side == "buy":
                    fill_price = current_price * (1 - SLIPPAGE_BPS / 10000)
                else:
                    fill_price = current_price * (1 + SLIPPAGE_BPS / 10000)
                pnl_pct = _compute_pnl_pct(position["entry_price"], fill_price, side)
                r_mult = pnl_pct / (STOP_PCT * 100) if STOP_PCT > 0 else 0.0
                trades.append({
                    "entry_bar": position["entry_bar"],
                    "exit_bar": bar_idx,
                    "entry_price": position["entry_price"],
                    "exit_price": fill_price,
                    "side": side,
                    "pnl_pct": pnl_pct,
                    "r_multiple": round(r_mult, 4),
                    "bars_held": bar_idx - position["entry_bar"],
                    "exit_reason": "signal_exit",
                })
                position = None
                continue

        # ── Check entry when flat ─────────────────────────────────────────
        if position is None and signal["entry"]:
            side = signal["side"]
            if side == "buy":
                fill_price = current_price * (1 + SLIPPAGE_BPS / 10000)
                stop = fill_price * (1 - STOP_PCT)
            else:
                fill_price = current_price * (1 - SLIPPAGE_BPS / 10000)
                stop = fill_price * (1 + STOP_PCT)
            position = {
                "entry_bar": bar_idx,
                "entry_price": fill_price,
                "side": side,
                "stop_loss": stop,
            }

    # Force-close any open position at last bar
    if position is not None:
        final_price = close[-1]
        side = position["side"]
        if side == "buy":
            fill_price = final_price * (1 - SLIPPAGE_BPS / 10000)
        else:
            fill_price = final_price * (1 + SLIPPAGE_BPS / 10000)
        pnl_pct = _compute_pnl_pct(position["entry_price"], fill_price, side)
        r_mult = pnl_pct / (STOP_PCT * 100) if STOP_PCT > 0 else 0.0
        trades.append({
            "entry_bar": position["entry_bar"],
            "exit_bar": n - 1,
            "entry_price": position["entry_price"],
            "exit_price": fill_price,
            "side": side,
            "pnl_pct": pnl_pct,
            "r_multiple": round(r_mult, 4),
            "bars_held": (n - 1) - position["entry_bar"],
            "exit_reason": "end_of_data",
        })

    return trades


def _compute_pnl_pct(entry_price: float, exit_price: float, side: str) -> float:
    """Return PnL as a percentage (e.g. 2.5 means +2.5%)."""
    if entry_price <= 0:
        return 0.0
    if side == "buy":
        return ((exit_price - entry_price) / entry_price) * 100
    else:
        return ((entry_price - exit_price) / entry_price) * 100


# ── Metrics ───────────────────────────────────────────────────────────────────

def _compute_metrics(trades: list[dict]) -> dict:
    """Compute backtest metrics from a list of completed trades."""
    if not trades:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "total_pnl_pct": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
        }

    pnls = [t["pnl_pct"] for t in trades]
    r_multiples = [t["r_multiple"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    trade_count = len(trades)
    win_rate = len(wins) / trade_count if trade_count > 0 else 0.0
    avg_r = sum(r_multiples) / trade_count if trade_count > 0 else 0.0
    total_pnl_pct = sum(pnls)

    gross_wins = sum(wins) if wins else 0.0
    gross_losses = abs(sum(losses)) if losses else 0.0
    profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else (
        float("inf") if gross_wins > 0 else 0.0
    )

    # Max drawdown (peak-to-trough on cumulative equity)
    max_drawdown = _compute_max_drawdown(pnls)

    # Sharpe ratio: mean daily return / std daily return * sqrt(252)
    # Treat each trade's PnL as a "daily return" proxy
    sharpe = _compute_sharpe(pnls)

    return {
        "trade_count": trade_count,
        "win_rate": round(win_rate, 4),
        "avg_r": round(avg_r, 4),
        "total_pnl_pct": round(total_pnl_pct, 4),
        "profit_factor": round(min(profit_factor, 99.0), 4),  # cap for display
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4),
    }


def _compute_max_drawdown(pnls: list[float]) -> float:
    """Max drawdown from a series of trade PnLs (percentage-based)."""
    if not pnls:
        return 0.0
    equity = 100.0  # start at 100%
    peak = equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _compute_sharpe(pnls: list[float], annualize_factor: float = 252.0) -> float:
    """Sharpe ratio: (mean return / std return) * sqrt(annualize_factor)."""
    if len(pnls) < 2:
        return 0.0
    mean_ret = statistics.mean(pnls)
    std_ret = statistics.stdev(pnls)
    if std_ret == 0:
        return 0.0
    return (mean_ret / std_ret) * math.sqrt(annualize_factor)


# ── In-sample / Out-of-sample split ──────────────────────────────────────────

def _split_is_oos(trades: list[dict], is_ratio: float = 0.70) -> tuple[list, list]:
    """Split trades into in-sample (first 70%) and out-of-sample (last 30%)."""
    split_idx = int(len(trades) * is_ratio)
    return trades[:split_idx], trades[split_idx:]


# ── Monte Carlo simulation ───────────────────────────────────────────────────

def _monte_carlo(trades: list[dict], n_sims: int = 500) -> dict:
    """
    Shuffle trade PnLs, compute worst drawdown per path.
    Returns P50 and P95 drawdown, plus risk classification.
    """
    if len(trades) < 5:
        return {
            "mc_p50_dd": 0.0,
            "mc_p95_dd": 0.0,
            "mc_risk_class": "low",
        }

    pnls = [t["pnl_pct"] for t in trades]
    worst_dds = []

    for _ in range(n_sims):
        shuffled = pnls[:]
        random.shuffle(shuffled)
        dd = _compute_max_drawdown(shuffled)
        worst_dds.append(dd)

    worst_dds.sort()
    p50_idx = int(len(worst_dds) * 0.50)
    p95_idx = int(len(worst_dds) * 0.95)
    p50_dd = worst_dds[min(p50_idx, len(worst_dds) - 1)]
    p95_dd = worst_dds[min(p95_idx, len(worst_dds) - 1)]

    if p95_dd < 0.05:
        risk_class = "low"
    elif p95_dd < 0.10:
        risk_class = "medium"
    else:
        risk_class = "high"

    return {
        "mc_p50_dd": round(p50_dd, 6),
        "mc_p95_dd": round(p95_dd, 6),
        "mc_risk_class": risk_class,
    }


# ── Scoring ───────────────────────────────────────────────────────────────────

def _score_strategy(oos_metrics: dict, full_metrics: dict, mc: dict) -> int:
    """
    Score a strategy 0-100:
      OOS Sharpe contribution (30%)
      Win rate contribution (20%)
      Profit factor contribution (20%)
      Drawdown contribution (30%)
    """
    oos_sharpe = oos_metrics.get("sharpe", 0.0)
    win_rate = full_metrics.get("win_rate", 0.0)
    pf = full_metrics.get("profit_factor", 0.0)
    mc_p95_dd = mc.get("mc_p95_dd", 1.0)

    # OOS Sharpe: 0 at <=0, 30 at Sharpe >= 2.0
    sharpe_score = max(0.0, min(oos_sharpe / 2.0, 1.0)) * 30.0

    # Win rate: 0 at <=30%, 20 at >=65%
    wr_norm = max(0.0, min((win_rate - 0.30) / 0.35, 1.0)) * 20.0

    # Profit factor: 0 at <=1.0, 20 at >=3.0
    pf_norm = max(0.0, min((pf - 1.0) / 2.0, 1.0)) * 20.0

    # Drawdown: 30 at 0% MC P95, 0 at >=15%
    dd_norm = max(0.0, min((0.15 - mc_p95_dd) / 0.15, 1.0)) * 30.0

    return int(round(sharpe_score + wr_norm + pf_norm + dd_norm))


# ── Quality gates ─────────────────────────────────────────────────────────────

def _check_quality_gates(
    full_metrics: dict,
    oos_metrics: dict,
    mc: dict,
    min_trades: int,
) -> tuple[bool, list[str]]:
    """Return (passed, list_of_failure_reasons)."""
    failures = []

    if full_metrics["trade_count"] < min_trades:
        failures.append(
            f"trade_count {full_metrics['trade_count']} < {min_trades}"
        )

    pf = full_metrics["profit_factor"]
    if pf < 1.5 or pf > 5.0:
        failures.append(f"profit_factor {pf:.2f} outside [1.5, 5.0]")

    mc_p95 = mc["mc_p95_dd"]
    if mc_p95 > 0.08:
        failures.append(f"mc_p95_dd {mc_p95:.4f} > 0.08")

    oos_sharpe = oos_metrics.get("sharpe", 0.0)
    if oos_sharpe <= 0.3:
        failures.append(f"oos_sharpe {oos_sharpe:.4f} <= 0.3")

    return (len(failures) == 0, failures)


# ── Projected monthly return ──────────────────────────────────────────────────

def _projected_monthly_pct(trades: list[dict], total_bars: int) -> float:
    """Estimate monthly return from total PnL and bar count."""
    if not trades or total_bars <= 0:
        return 0.0
    total_pnl = sum(t["pnl_pct"] for t in trades)
    # Assume ~21 trading days/month for daily, scale for other timeframes
    return round(total_pnl / total_bars * 21, 4)


# ── Main entry point ──────────────────────────────────────────────────────────

def run(strategies: list, timeframe: str = "1d", min_trades: int = 100) -> dict:
    """
    Backtest each strategy against historical data for all 4 instruments.

    Args:
        strategies: list of strategy dicts, each with at least 'id' or 'strategy_id'
                    and 'strategy_name'
        timeframe:  candle timeframe ("1d", "4h", "1h", "15m")
        min_trades: minimum trade count to pass quality gate

    Returns:
        {
            "status": "success",
            "all_scores": [...],
            "winners": [...],
            "summary": "Tested 5 strategies: 2 passed, best score 78",
            "metrics": {"tested": 5, "passed": 2, "best_score": 78},
        }
    """
    if not strategies:
        return {
            "status": "failed",
            "all_scores": [],
            "winners": [],
            "summary": "No strategies provided for testing.",
            "metrics": {"tested": 0, "passed": 0, "best_score": 0},
        }

    log.info("Strategy tester: testing %d strategies on %s timeframe", len(strategies), timeframe)

    # ── Fetch OHLCV for all instruments once ──────────────────────────────
    instrument_data = {}
    for name, ticker in INSTRUMENTS.items():
        ohlcv = _fetch_backtest_ohlcv(ticker, timeframe)
        if ohlcv and len(ohlcv.get("close", [])) > 50:
            instrument_data[name] = ohlcv
            log.info("  %s (%s): %d bars fetched", name, ticker, len(ohlcv["close"]))
        else:
            log.warning("  %s (%s): insufficient data, skipping", name, ticker)

    if not instrument_data:
        return {
            "status": "failed",
            "all_scores": [],
            "winners": [],
            "summary": "Failed to fetch OHLCV data for any instrument.",
            "metrics": {"tested": 0, "passed": 0, "best_score": 0},
        }

    # ── Test each strategy ────────────────────────────────────────────────
    all_scores = []

    for strategy in strategies:
        strat_id = (
            strategy.get("id")
            or strategy.get("strategy_id")
            or strategy.get("strategy_name", "unknown")
        )
        strat_name = strategy.get("strategy_name") or strategy.get("name") or strat_id
        log.info("  Testing: %s", strat_name)

        # Aggregate trades across all instruments
        all_trades = []
        total_bars = 0

        for inst_name, ohlcv in instrument_data.items():
            trades = _simulate_trades(strategy, ohlcv)
            all_trades.extend(trades)
            total_bars += len(ohlcv["close"])
            log.debug("    %s: %d trades", inst_name, len(trades))

        # Sort trades by entry_bar to maintain chronological order across instruments
        all_trades.sort(key=lambda t: t["entry_bar"])

        # Full metrics
        full_metrics = _compute_metrics(all_trades)

        # In-sample / Out-of-sample split
        is_trades, oos_trades = _split_is_oos(all_trades)
        is_metrics = _compute_metrics(is_trades)
        oos_metrics = _compute_metrics(oos_trades)

        # Monte Carlo
        mc = _monte_carlo(all_trades, n_sims=500)

        # Score
        score = _score_strategy(oos_metrics, full_metrics, mc)

        # Quality gates
        passed, gate_failures = _check_quality_gates(full_metrics, oos_metrics, mc, min_trades)

        # Projected monthly
        proj_monthly = _projected_monthly_pct(all_trades, total_bars)

        result = {
            "strategy_id": strat_id,
            "strategy_name": strat_name,
            "passed": passed,
            "trade_count": full_metrics["trade_count"],
            "win_rate": full_metrics["win_rate"],
            "avg_r": full_metrics["avg_r"],
            "profit_factor": full_metrics["profit_factor"],
            "max_drawdown": full_metrics["max_drawdown"],
            "sharpe": full_metrics["sharpe"],
            "oos_sharpe": oos_metrics["sharpe"],
            "oos_win_rate": oos_metrics["win_rate"],
            "oos_trade_count": oos_metrics["trade_count"],
            "mc_p95_dd": mc["mc_p95_dd"],
            "mc_risk_class": mc["mc_risk_class"],
            "score": score,
            "projected_monthly_pct": proj_monthly,
            "gate_failures": gate_failures,
        }

        all_scores.append(result)
        log.info(
            "    %s: %d trades, WR=%.1f%%, Sharpe=%.2f, OOS_Sharpe=%.2f, PF=%.2f, "
            "MC_P95=%.3f, Score=%d, Passed=%s",
            strat_name,
            full_metrics["trade_count"],
            full_metrics["win_rate"] * 100,
            full_metrics["sharpe"],
            oos_metrics["sharpe"],
            full_metrics["profit_factor"],
            mc["mc_p95_dd"],
            score,
            passed,
        )

    # ── Sort and select winners ───────────────────────────────────────────
    winners = sorted(
        [s for s in all_scores if s["passed"]],
        key=lambda s: s["score"],
        reverse=True,
    )

    best_score = max((s["score"] for s in all_scores), default=0)
    tested = len(all_scores)
    passed_count = len(winners)

    summary = (
        f"Tested {tested} strategies: {passed_count} passed, "
        f"best score {best_score}"
    )
    if winners:
        summary += f" ({winners[0]['strategy_name']})"

    log.info("Strategy tester complete: %s", summary)

    return {
        "status": "success",
        "all_scores": all_scores,
        "winners": winners,
        "summary": summary,
        "metrics": {
            "tested": tested,
            "passed": passed_count,
            "best_score": best_score,
        },
    }
