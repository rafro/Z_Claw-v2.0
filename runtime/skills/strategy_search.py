"""
strategy-search skill — Tier 0+1 hybrid (LLM via strategy_builder, pure Python testing).

Self-contained search loop that generates candidate strategies and backtests them
until one meets prop-firm compliance thresholds. Replaces the external
scripts/search_target_strategies.py dependency on agent-network.

ON DEMAND ONLY — never auto-triggered by cron. Strategy search is expensive
(multiple LLM calls + backtesting per attempt). Triggered manually when the
current strategy's health degrades.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime.config import ROOT, STATE_DIR

log = logging.getLogger(__name__)

HISTORY_PATH = STATE_DIR / "strategy-search-history.json"
ACTIVE_STRATEGY_PATH = STATE_DIR / "active-strategy.json"
REPORTS_DIR = ROOT / "reports"

# ── Default thresholds ────────────────────────────────────────────────────────
MIN_TRADE_COUNT = 100


def run(
    max_attempts: int = 10,
    timeframe: str = "1d",
    min_monthly: float = 0.02,
    min_pf: float = 2.0,
    max_pf: float = 4.0,
    prop_firm_dd: float = 0.08,
) -> dict:
    """
    Search loop: generate strategies via strategy_builder, test via strategy_tester,
    repeat until a prop-firm-compliant strategy is found or attempts are exhausted.

    Returns a result dict compatible with the trading orchestrator packet format:
    {
        "status":             "success" | "partial",
        "winner":             dict | None,
        "best_near_miss":     dict | None,
        "attempts":           list[dict],
        "total_tested":       int,
        "summary":            str,
        "metrics":            dict,
        "escalate":           bool,
        "escalation_reason":  str,
    }
    """
    history = _load_search_history()
    feedback = _build_feedback(history)
    retired = history.get("retired_patterns", [])

    best_seen: Optional[dict] = None
    attempts: list[dict] = []

    for attempt in range(1, max_attempts + 1):
        log.info(
            "[strategy-search] attempt %d/%d — timeframe=%s",
            attempt, max_attempts, timeframe,
        )

        # ── Step 1: Generate candidate strategies ─────────────────────────
        try:
            from runtime.skills import strategy_builder
            build_result = strategy_builder.run(
                num_strategies=5,
                timeframe=timeframe,
                feedback=feedback,
                retired_patterns=retired,
            )
        except Exception as e:
            log.error("[strategy-search] builder failed on attempt %d: %s", attempt, e)
            attempts.append({"attempt": attempt, "generated": 0, "passed": 0, "error": str(e)})
            continue

        strategies = build_result.get("strategies", [])
        if not strategies:
            log.warning("[strategy-search] builder returned 0 strategies on attempt %d", attempt)
            attempts.append({"attempt": attempt, "generated": 0, "passed": 0})
            continue

        # ── Step 2: Backtest all candidates ───────────────────────────────
        try:
            from runtime.skills import strategy_tester
            test_result = strategy_tester.run(
                strategies=strategies,
                timeframe=timeframe,
            )
        except Exception as e:
            log.error("[strategy-search] tester failed on attempt %d: %s", attempt, e)
            attempts.append({
                "attempt": attempt,
                "generated": len(strategies),
                "passed": 0,
                "error": str(e),
            })
            continue

        # ── Step 3: Check candidates against prop-firm thresholds ─────────
        all_scores = test_result.get("all_scores", [])
        winners: list[dict] = []

        for candidate in all_scores:
            if _candidate_matches(candidate, min_monthly, min_pf, max_pf, prop_firm_dd):
                winners.append(candidate)

        # ── Step 4: Update feedback for next cycle ────────────────────────
        _update_history(history, test_result, attempt)
        feedback = _build_feedback(history)
        retired = history.get("retired_patterns", [])

        # ── Step 5: Track best near-miss ──────────────────────────────────
        for candidate in all_scores:
            penalty = _calc_penalty(candidate, min_monthly, min_pf, max_pf, prop_firm_dd)
            if best_seen is None or penalty < best_seen["_penalty"]:
                best_seen = {**candidate, "_penalty": penalty}

        attempts.append({
            "attempt": attempt,
            "generated": len(strategies),
            "tested": len(all_scores),
            "passed": len(winners),
        })

        log.info(
            "[strategy-search] attempt %d: generated=%d tested=%d passed=%d",
            attempt, len(strategies), len(all_scores), len(winners),
        )

        # ── Step 6: If we have a winner, stop ─────────────────────────────
        if winners:
            winner = sorted(winners, key=lambda w: -float(w.get("score", 0) or 0))[0]
            _save_active_strategy(winner)
            _save_search_history(history)
            _write_report("matched", attempt, winner, best_seen, attempts, {
                "min_monthly": min_monthly,
                "min_pf": min_pf,
                "max_pf": max_pf,
                "prop_firm_dd": prop_firm_dd,
                "timeframe": timeframe,
            })

            total_tested = sum(a.get("generated", 0) for a in attempts)
            return {
                "status": "success",
                "winner": _sanitize_candidate(winner),
                "best_near_miss": None,
                "attempts": attempts,
                "total_tested": total_tested,
                "summary": (
                    f"Found prop-firm-compliant strategy on attempt {attempt}: "
                    f"{winner.get('strategy_name', winner.get('name', '?'))} "
                    f"(score {winner.get('score', 0)})"
                ),
                "metrics": _extract_winner_metrics(winner),
                "escalate": False,
                "escalation_reason": "",
            }

    # ── No match found ────────────────────────────────────────────────────────
    _save_search_history(history)
    total_tested = sum(a.get("generated", 0) for a in attempts)
    near_miss_name = ""
    if best_seen:
        near_miss_name = best_seen.get("strategy_name", best_seen.get("name", ""))

    _write_report("no_match", max_attempts, None, best_seen, attempts, {
        "min_monthly": min_monthly,
        "min_pf": min_pf,
        "max_pf": max_pf,
        "prop_firm_dd": prop_firm_dd,
        "timeframe": timeframe,
    })

    return {
        "status": "partial",
        "winner": None,
        "best_near_miss": _sanitize_candidate(best_seen) if best_seen else None,
        "attempts": attempts,
        "total_tested": total_tested,
        "summary": (
            f"No prop-firm-compliant strategy found in {max_attempts} attempts "
            f"({total_tested} strategies tested). "
            f"Best near-miss: {near_miss_name or 'none'}"
        ),
        "metrics": _extract_near_miss_metrics(best_seen) if best_seen else {},
        "escalate": True,
        "escalation_reason": (
            "Strategy search exhausted without finding prop-firm-compliant strategy"
        ),
    }


# ── Candidate matching ────────────────────────────────────────────────────────

def _candidate_matches(
    candidate: dict,
    min_monthly: float,
    min_pf: float,
    max_pf: float,
    prop_firm_dd: float,
) -> bool:
    """
    Check whether a candidate strategy meets all prop-firm compliance thresholds:
    - trade_count >= 100
    - profit_factor in [min_pf, max_pf]
    - mc_p95_dd <= prop_firm_dd (Monte Carlo 95th percentile drawdown)
    - projected_monthly_pnl_pct >= min_monthly
    """
    if not candidate.get("passed"):
        return False

    trade_count = int(candidate.get("trade_count", 0) or 0)
    if trade_count < MIN_TRADE_COUNT:
        return False

    profit_factor = float(candidate.get("profit_factor", 0.0) or 0.0)
    if profit_factor < min_pf or profit_factor > max_pf:
        return False

    mc_p95_dd = candidate.get("mc_p95_dd")
    if mc_p95_dd is None or float(mc_p95_dd) > prop_firm_dd:
        return False

    projected_monthly = float(candidate.get("projected_monthly_pnl_pct", 0.0) or 0.0)
    if projected_monthly < min_monthly:
        return False

    return True


def _calc_penalty(
    candidate: dict,
    min_monthly: float,
    min_pf: float,
    max_pf: float,
    prop_firm_dd: float,
) -> float:
    """
    Calculate total distance from targets for near-miss ranking.
    Lower is closer to passing. Returns a single scalar for comparison.
    """
    mc_p95_dd = float(candidate.get("mc_p95_dd", 1.0) or 1.0)
    profit_factor = float(candidate.get("profit_factor", 0.0) or 0.0)
    projected_monthly = float(candidate.get("projected_monthly_pnl_pct", 0.0) or 0.0)
    trade_count = int(candidate.get("trade_count", 0) or 0)

    dd_penalty = max(0.0, mc_p95_dd - prop_firm_dd)

    if profit_factor < min_pf:
        pf_penalty = min_pf - profit_factor
    elif profit_factor > max_pf:
        pf_penalty = profit_factor - max_pf
    else:
        pf_penalty = 0.0

    monthly_penalty = max(0.0, min_monthly - projected_monthly)
    trade_penalty = max(0.0, (MIN_TRADE_COUNT - trade_count) / MIN_TRADE_COUNT) if trade_count < MIN_TRADE_COUNT else 0.0

    # Weight drawdown heavily — prop firm disqualification is the hardest constraint
    return (dd_penalty * 5.0) + (pf_penalty * 2.0) + (monthly_penalty * 3.0) + trade_penalty


# ── Search history persistence ────────────────────────────────────────────────

def _load_search_history() -> dict:
    """Read search history from state/strategy-search-history.json."""
    if not HISTORY_PATH.exists():
        return {
            "runs": [],
            "performance_history": [],
            "retired_patterns": [],
            "asset_strategy_log": [],
            "last_updated": None,
        }
    try:
        with open(HISTORY_PATH, encoding="utf-8-sig") as f:
            data = json.load(f)
        # Ensure all expected keys
        data.setdefault("runs", [])
        data.setdefault("performance_history", [])
        data.setdefault("retired_patterns", [])
        data.setdefault("asset_strategy_log", [])
        return data
    except Exception as e:
        log.error("Failed to load search history: %s", e)
        return {
            "runs": [],
            "performance_history": [],
            "retired_patterns": [],
            "asset_strategy_log": [],
            "last_updated": None,
        }


def _save_search_history(history: dict) -> None:
    """Atomic write to state/strategy-search-history.json."""
    history["last_updated"] = datetime.now(timezone.utc).isoformat()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(HISTORY_PATH, history)


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        suffix=".tmp",
        prefix=path.stem + "_",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, str(path))
    except Exception:
        # Clean up on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Feedback builder ──────────────────────────────────────────────────────────

def _build_feedback(history: dict) -> Optional[dict]:
    """
    Build coaching feedback from search history for the strategy builder.
    Same logic as the external search_target_strategies.py: best indicators,
    avg R, failing patterns, and a next-cycle hypothesis.
    """
    perf = history.get("performance_history", [])
    if len(perf) < 3:
        return None

    # Gather recent winners (those with positive OOS sharpe + not low confidence)
    recent: list[dict] = []
    for entry in reversed(perf):
        if float(entry.get("oos_sharpe", 0.0) or 0.0) <= 0:
            continue
        if str(entry.get("confidence_rating", "")).lower() in ("low", "very low"):
            continue
        recent.append(entry)
        if len(recent) >= 20:
            break

    if len(recent) < 3:
        return None

    # Count best indicator types
    counts: dict[str, int] = {}
    for entry in recent:
        indicator = (
            entry.get("primary_indicator", "")
            or entry.get("strategy_schema", {}).get("primary_indicator", {}).get("type", "")
        )
        if indicator:
            counts[indicator] = counts.get(indicator, 0) + 1

    best_indicator = max(counts, key=counts.get) if counts else ""
    avg_r = sum(float(e.get("avg_r", 0.0) or 0.0) for e in recent) / len(recent)

    # Identify failing patterns from strategy log
    strat_log = history.get("asset_strategy_log", [])
    fail_counts: dict[str, int] = {}
    for entry in strat_log[-100:]:
        if entry.get("result") == "failed":
            pattern = entry.get("primary_indicator", "unknown")
            fail_counts[pattern] = fail_counts.get(pattern, 0) + 1
    top_failures = sorted(fail_counts.items(), key=lambda x: -x[1])[:3]
    failing_patterns = [p for p, _ in top_failures]

    return {
        "best_indicator_types": [best_indicator] if best_indicator else [],
        "best_rr_ratio": round(avg_r, 2),
        "failing_regimes": failing_patterns,
        "next_cycle_hypothesis": (
            f"Recent winners favour {best_indicator} with avg R {avg_r:.2f}. "
            f"Avoid patterns: {', '.join(failing_patterns)}."
            if best_indicator else
            f"Recent winners average {avg_r:.2f}R per trade. "
            f"Avoid patterns: {', '.join(failing_patterns)}."
        ),
    }


# ── History update ────────────────────────────────────────────────────────────

def _update_history(history: dict, test_result: dict, attempt: int) -> None:
    """
    Append test results to search history. Retire patterns that consistently fail.
    """
    all_scores = test_result.get("all_scores", [])
    now_iso = datetime.now(timezone.utc).isoformat()

    # Append individual scores to the strategy log
    for score in all_scores:
        entry = {
            "attempt": attempt,
            "timestamp": now_iso,
            "strategy_name": score.get("strategy_name", score.get("name", "")),
            "primary_indicator": score.get("primary_indicator", ""),
            "entry_trigger": score.get("entry_trigger", ""),
            "confirmation": score.get("confirmation", ""),
            "result": "passed" if score.get("passed") else "failed",
            "failure_reason": score.get("failure", ""),
            "score": score.get("score", 0),
            "sharpe": score.get("sharpe", 0),
            "oos_sharpe": score.get("oos_sharpe", 0),
            "win_rate": score.get("win_rate", 0),
            "max_drawdown_pct": score.get("max_drawdown_pct", 0),
            "total_pnl_pct": score.get("total_pnl_pct", 0),
            "mc_p95_dd": score.get("mc_p95_dd"),
            "mc_risk_class": score.get("mc_risk_class"),
            "trade_count": score.get("trade_count", 0),
            "profit_factor": score.get("profit_factor", 0),
            "projected_monthly_pnl_pct": score.get("projected_monthly_pnl_pct", 0),
            "avg_r": score.get("avg_r", 0),
        }
        history["asset_strategy_log"].append(entry)

    # Winners go into performance_history
    winners = test_result.get("winners", [])
    for winner in winners:
        history["performance_history"].append({
            **winner,
            "attempt": attempt,
            "timestamp": now_iso,
        })

    # Retire patterns that have failed 5+ times in the last 50 entries
    recent_log = history["asset_strategy_log"][-50:]
    fail_counts: dict[str, int] = {}
    for entry in recent_log:
        if entry.get("result") == "failed":
            pattern = entry.get("primary_indicator", "")
            if pattern:
                fail_counts[pattern] = fail_counts.get(pattern, 0) + 1

    for pattern, count in fail_counts.items():
        if count >= 5 and pattern not in history["retired_patterns"]:
            history["retired_patterns"].append(pattern)
            log.info("[strategy-search] retired pattern: %s (failed %d times)", pattern, count)

    # Record the run
    history["runs"].append({
        "attempt": attempt,
        "timestamp": now_iso,
        "strategies_tested": len(all_scores),
        "winners_found": len(winners),
    })


# ── Active strategy persistence ───────────────────────────────────────────────

def _save_active_strategy(winner: dict) -> None:
    """
    Write winning strategy to state/active-strategy.json so virtual_account
    and other trading skills can consume it.
    """
    payload = {
        "strategy_name": winner.get("strategy_name", winner.get("name", "")),
        "strategy_id": winner.get("strategy_id", winner.get("id", "")),
        "score": winner.get("score"),
        "sharpe": winner.get("sharpe"),
        "oos_sharpe": winner.get("oos_sharpe"),
        "win_rate": winner.get("win_rate"),
        "profit_factor": winner.get("profit_factor"),
        "avg_r": winner.get("avg_r"),
        "trade_count": winner.get("trade_count"),
        "mc_p95_dd": winner.get("mc_p95_dd"),
        "mc_risk_class": winner.get("mc_risk_class"),
        "max_drawdown_pct": winner.get("max_drawdown_pct"),
        "projected_monthly_pnl_pct": winner.get("projected_monthly_pnl_pct"),
        "confidence_rating": winner.get("confidence_rating", ""),
        "direction": winner.get("direction", ""),
        "strategy_schema": winner.get("strategy_schema"),
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "source": "strategy_search",
    }
    _atomic_write_json(ACTIVE_STRATEGY_PATH, payload)
    log.info(
        "[strategy-search] saved active strategy: %s (score=%s)",
        payload["strategy_name"], payload["score"],
    )


# ── Metrics extraction ────────────────────────────────────────────────────────

def _extract_winner_metrics(winner: dict) -> dict:
    """Extract key metrics from a winning strategy for the packet."""
    return {
        "strategy_name": winner.get("strategy_name", winner.get("name", "")),
        "strategy_id": winner.get("strategy_id", winner.get("id", "")),
        "score": winner.get("score"),
        "sharpe": winner.get("sharpe"),
        "oos_sharpe": winner.get("oos_sharpe"),
        "win_rate": winner.get("win_rate"),
        "profit_factor": winner.get("profit_factor"),
        "avg_r": winner.get("avg_r"),
        "trade_count": winner.get("trade_count"),
        "mc_p95_dd": winner.get("mc_p95_dd"),
        "projected_monthly_pnl_pct": winner.get("projected_monthly_pnl_pct"),
    }


def _extract_near_miss_metrics(candidate: dict) -> dict:
    """Extract key metrics from the best near-miss for reporting."""
    return {
        "near_miss_name": candidate.get("strategy_name", candidate.get("name", "")),
        "near_miss_score": candidate.get("score"),
        "near_miss_pf": candidate.get("profit_factor"),
        "near_miss_mc_p95_dd": candidate.get("mc_p95_dd"),
        "near_miss_monthly": candidate.get("projected_monthly_pnl_pct"),
        "near_miss_penalty": candidate.get("_penalty"),
    }


def _sanitize_candidate(candidate: Optional[dict]) -> Optional[dict]:
    """Remove internal keys (prefixed with _) before returning to caller."""
    if candidate is None:
        return None
    return {k: v for k, v in candidate.items() if not k.startswith("_")}


# ── Report writer ─────────────────────────────────────────────────────────────

def _write_report(
    status: str,
    attempt: int,
    winner: Optional[dict],
    best_seen: Optional[dict],
    attempts: list[dict],
    criteria: dict,
) -> Path:
    """Write a JSON search report to reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = REPORTS_DIR / f"strategy-search-{stamp}.json"

    payload = {
        "status": status,
        "criteria": criteria,
        "matched_on_attempt": attempt if status == "matched" else None,
        "matched_strategy": _sanitize_candidate(winner) if winner else None,
        "best_candidate_seen": _sanitize_candidate(best_seen) if best_seen else None,
        "attempts": attempts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        _atomic_write_json(out_path, payload)
        log.info("[strategy-search] report written: %s", out_path)
    except Exception as e:
        log.error("[strategy-search] failed to write report: %s", e)

    return out_path
