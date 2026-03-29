import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


JCLAW_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AGENT_NETWORK_ROOT = Path(r"C:\Users\Tyler\agent-network")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Loop the agent-network Strategy Builder + Backtester until a strategy "
            "meets target monthly return, profit factor, and prop-firm drawdown limits."
        )
    )
    parser.add_argument(
        "--agent-network-root",
        default=str(DEFAULT_AGENT_NETWORK_ROOT),
        help="Path to the agent-network repo.",
    )
    parser.add_argument(
        "--asset-chain",
        default=None,
        help="Override ASSET_CHAIN from agent-network .env.",
    )
    parser.add_argument(
        "--asset-token-address",
        default=None,
        help="Override ASSET_TOKEN_ADDRESS from agent-network .env.",
    )
    parser.add_argument(
        "--bucket-seconds",
        type=int,
        default=None,
        help="Override ASSET_BUCKET_SECONDS from agent-network .env.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=25,
        help="Maximum builder/backtester attempts before stopping.",
    )
    parser.add_argument(
        "--builder-total",
        type=int,
        default=None,
        help="Override BUILDER_TOTAL for this search run.",
    )
    parser.add_argument(
        "--builder-batch-size",
        type=int,
        default=None,
        help="Override BUILDER_BATCH_SIZE for this search run.",
    )
    parser.add_argument(
        "--min-monthly",
        type=float,
        default=0.02,
        help="Minimum projected monthly return as a fraction. Default 0.02 = 2%%.",
    )
    parser.add_argument(
        "--min-profit-factor",
        type=float,
        default=2.0,
        help="Minimum profit factor.",
    )
    parser.add_argument(
        "--max-profit-factor",
        type=float,
        default=4.0,
        help="Maximum profit factor.",
    )
    parser.add_argument(
        "--prop-firm-limit",
        type=float,
        choices=(5.0, 8.0, 10.0),
        default=8.0,
        help="Maximum allowed Monte Carlo p95 drawdown percentage.",
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore existing cycle-state history and start the search cold.",
    )
    return parser.parse_args()


def _prepare_imports(agent_root: Path) -> None:
    if not agent_root.exists():
        raise FileNotFoundError(f"agent-network root not found: {agent_root}")
    os.chdir(agent_root)
    sys.path.insert(0, str(agent_root))


def _build_client(local_llm: bool, local_base_url: str, anthropic_api_key: str):
    if local_llm:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "LOCAL_LLM=true but the 'openai' package is not installed in this Python environment."
            ) from exc
        return OpenAI(
            base_url=local_base_url,
            api_key="local",
            timeout=1200,
        )

    from anthropic import Anthropic
    return Anthropic(api_key=anthropic_api_key)


def _build_feedback(performance_history: list[dict], bucket_seconds: int) -> dict | None:
    if len(performance_history) < 5:
        return None

    recent: list[dict] = []
    for winner in reversed(performance_history):
        schema = winner.get("strategy_schema", {})
        tf = str(schema.get("metadata", {}).get("timeframe", "")).lower()
        if bucket_seconds <= 3600:
            if "1h" not in tf:
                continue
        elif bucket_seconds <= 14400:
            if "4h" not in tf:
                continue
        else:
            if "1d" not in tf and "daily" not in tf:
                continue
        if winner.get("oos_sharpe", 0.0) <= 0:
            continue
        if str(winner.get("confidence_rating", "")).lower() == "low":
            continue
        recent.append(winner)
        if len(recent) >= 20:
            break

    if len(recent) < 3:
        return None

    counts: dict[str, int] = {}
    for winner in recent:
        indicator = winner.get("strategy_schema", {}).get("primary_indicator", {}).get("type", "")
        if indicator:
            counts[indicator] = counts.get(indicator, 0) + 1

    best_indicator = max(counts, key=counts.get) if counts else ""
    avg_r = sum(float(w.get("avg_r", 0.0) or 0.0) for w in recent) / len(recent)
    return {
        "best_indicator_types": [best_indicator] if best_indicator else [],
        "best_rr_ratio": round(avg_r, 2),
        "failing_regimes": [],
        "next_cycle_hypothesis": (
            f"Recent winners favour {best_indicator} with avg R {avg_r:.2f}."
            if best_indicator else
            f"Recent winners average {avg_r:.2f}R per trade."
        ),
    }


def _append_search_history(state, bt_data: dict, cycle_num: int) -> None:
    winners = bt_data.get("winners", [])
    if winners:
        state.active_strategy = winners[0]
        state.active_strategies = winners
        state.performance_history.extend(winners)
        top = winners[0]
        state.projected_avg_r = float(top.get("avg_r", 0.0) or 0.0)
        state.projected_win_rate = float(top.get("win_rate", 0.0) or 0.0)
        for winner in winners:
            sid = winner.get("strategy_id", "")
            mc_dd = winner.get("mc_p95_dd")
            if sid and mc_dd is not None:
                state.mc_p95_dd_by_strategy[sid] = float(mc_dd)
            projected = winner.get("projected_monthly_pnl_pct")
            if sid and projected is not None:
                state.projected_monthly_pnl_pct_by_strategy[sid] = float(projected)
            projections = winner.get("return_projections")
            if sid and projections:
                state.return_projections_by_strategy[sid] = projections

    for score in bt_data.get("all_scores", []):
        state.asset_strategy_log.append({
            "cycle": cycle_num,
            "strategy_name": score.get("name", ""),
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
        })


def _project_monthly(project_returns_fn, candidate: dict, years_covered: float) -> float:
    trade_count = int(candidate.get("trade_count", 0) or 0)
    avg_r = float(candidate.get("avg_r", 0.0) or 0.0)
    risk_pct = float(candidate.get("best_risk_pct", 0.0) or 0.0)
    months = max(years_covered * 12.0, 1.0)
    trades_per_month = trade_count / months
    projections = project_returns_fn(avg_r=avg_r, risk_pct=risk_pct, trades_per_month=trades_per_month)
    return float(projections["1m"]["none"])


def _candidate_matches(
    candidate: dict,
    strategy_schema: dict | None,
    projected_monthly: float,
    args: argparse.Namespace,
) -> bool:
    trade_count = int(candidate.get("trade_count", 0) or 0)
    if trade_count < 100:
        return False  # Statistically meaningless with fewer than 100 trades

    if not candidate.get("passed"):
        return False
    if strategy_schema is None:
        return False

    mc_p95_dd = candidate.get("mc_p95_dd")
    if mc_p95_dd is None or float(mc_p95_dd) > (args.prop_firm_limit / 100.0):
        return False

    profit_factor = float(candidate.get("profit_factor", 0.0) or 0.0)
    if profit_factor < args.min_profit_factor or profit_factor > args.max_profit_factor:
        return False

    if projected_monthly < args.min_monthly:
        return False

    return True


def _candidate_penalty(candidate: dict, projected_monthly: float, args: argparse.Namespace) -> tuple[float, float, float]:
    mc_p95_dd = float(candidate.get("mc_p95_dd", 1.0) or 1.0)
    profit_factor = float(candidate.get("profit_factor", 0.0) or 0.0)

    dd_penalty = max(0.0, mc_p95_dd - (args.prop_firm_limit / 100.0))
    if profit_factor < args.min_profit_factor:
        pf_penalty = args.min_profit_factor - profit_factor
    elif profit_factor > args.max_profit_factor:
        pf_penalty = profit_factor - args.max_profit_factor
    else:
        pf_penalty = 0.0
    monthly_penalty = max(0.0, args.min_monthly - projected_monthly)

    return (
        round(dd_penalty, 6),
        round(pf_penalty, 6),
        round(monthly_penalty, 6),
    )


def _write_report(asset_key: str, payload: dict) -> Path:
    report_dir = JCLAW_ROOT / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = report_dir / f"strategy-search-{asset_key}-{stamp}.json"
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def main() -> int:
    args = _parse_args()
    agent_root = Path(args.agent_network_root).resolve()
    _prepare_imports(agent_root)

    from agents.asset_profile import build_asset_key
    from agents.backtester import BacktesterAgent
    from agents.strategy_builder import StrategyBuilderAgent
    from config.settings import (
        ANTHROPIC_API_KEY,
        ASSET_BUCKET_SECONDS,
        ASSET_CHAIN,
        ASSET_TOKEN_ADDRESS,
        LOCAL_LLM,
        LOCAL_LLM_BASE_URL,
    )
    from core.message import Message, MessageType
    from cycle.state import CycleState, load_state, state_path_for
    from backtesting.metrics import project_returns

    if args.builder_total is not None:
        os.environ["BUILDER_TOTAL"] = str(args.builder_total)
    if args.builder_batch_size is not None:
        os.environ["BUILDER_BATCH_SIZE"] = str(args.builder_batch_size)

    asset_cfg = {
        "chain": args.asset_chain or ASSET_CHAIN,
        "token_address": args.asset_token_address if args.asset_token_address is not None else ASSET_TOKEN_ADDRESS,
        "bucket_seconds": args.bucket_seconds or ASSET_BUCKET_SECONDS,
    }
    asset_key = build_asset_key(asset_cfg["chain"], asset_cfg["token_address"])
    state_path = state_path_for(asset_key)
    seed_state = CycleState() if args.fresh else load_state(state_path)
    seed_state.asset_key = asset_key

    client = _build_client(
        local_llm=LOCAL_LLM,
        local_base_url=LOCAL_LLM_BASE_URL,
        anthropic_api_key=ANTHROPIC_API_KEY,
    )

    best_seen: dict | None = None
    best_penalty: tuple[float, float, float] | None = None
    attempts: list[dict] = []

    for attempt in range(1, args.max_attempts + 1):
        cycle_num = int(seed_state.cycle_number or 0) + attempt
        coach_feedback = _build_feedback(seed_state.performance_history, asset_cfg["bucket_seconds"])

        builder_request = {
            "cycle": cycle_num,
            "asset": asset_cfg,
            "coach_feedback": coach_feedback,
            "retired_patterns": seed_state.retired_patterns,
            "asset_strategy_log": seed_state.asset_strategy_log,
        }

        builder = StrategyBuilderAgent(client)
        builder.receive(Message(
            sender="strategy_search",
            recipient="strategy_builder",
            type=MessageType.TASK,
            content=json.dumps(builder_request),
            metadata={"original_sender": "strategy_search"},
        ))
        builder_responses = builder.process()
        if not builder_responses or builder_responses[0].type == MessageType.ERROR:
            raise RuntimeError("Strategy Builder returned no usable response.")
        builder_output = json.loads(builder_responses[0].content)
        strategies = builder_output.get("strategies", [])
        if not strategies:
            raise RuntimeError("Strategy Builder returned zero strategies.")

        strategy_map = {strat.get("id", ""): strat for strat in strategies if strat.get("id")}

        backtester = BacktesterAgent(client)
        backtester.receive(Message(
            sender="strategy_search",
            recipient="backtester",
            type=MessageType.TASK,
            content=json.dumps({"strategies": strategies, "asset": asset_cfg}),
            metadata={
                "original_sender": "strategy_search",
                "past_failures": seed_state.retired_patterns,
            },
        ))
        backtester_responses = backtester.process()
        if not backtester_responses:
            raise RuntimeError("Backtester returned no response.")
        if backtester_responses[0].type == MessageType.ERROR:
            raise RuntimeError(backtester_responses[0].content)
        bt_data = json.loads(backtester_responses[0].content)

        period = bt_data.get("period", {})
        years_covered = float(period.get("years_covered", 1.0) or 1.0)
        matching_candidates: list[dict] = []

        for candidate in bt_data.get("all_scores", []):
            strategy_id = candidate.get("id", "")
            strategy_schema = strategy_map.get(strategy_id)
            projected_monthly = _project_monthly(project_returns, candidate, years_covered)
            penalty = _candidate_penalty(candidate, projected_monthly, args)

            enriched = {
                **candidate,
                "projected_monthly_pnl_pct": round(projected_monthly, 6),
                "strategy_schema": strategy_schema,
                "attempt": attempt,
                "cycle": cycle_num,
                "target_penalty": {
                    "drawdown": penalty[0],
                    "profit_factor": penalty[1],
                    "monthly_return": penalty[2],
                },
            }

            if best_penalty is None or penalty < best_penalty:
                best_penalty = penalty
                best_seen = enriched

            if _candidate_matches(candidate, strategy_schema, projected_monthly, args):
                matching_candidates.append(enriched)

        attempt_summary = {
            "attempt": attempt,
            "cycle": cycle_num,
            "generated": len(strategies),
            "passed": sum(1 for s in bt_data.get("all_scores", []) if s.get("passed")),
            "selection_note": bt_data.get("selection_note", ""),
            "matching_candidates": len(matching_candidates),
        }
        attempts.append(attempt_summary)
        _append_search_history(seed_state, bt_data, cycle_num)

        print(
            f"[attempt {attempt}/{args.max_attempts}] "
            f"generated={attempt_summary['generated']} "
            f"passed={attempt_summary['passed']} "
            f"matches={attempt_summary['matching_candidates']}"
        )

        if matching_candidates:
            matching_candidates.sort(
                key=lambda c: (
                    -float(c.get("projected_monthly_pnl_pct", 0.0)),
                    -float(c.get("score", 0.0) or 0.0),
                    float(c.get("mc_p95_dd", 1.0) or 1.0),
                )
            )
            winner = matching_candidates[0]
            report = {
                "status": "matched",
                "criteria": {
                    "min_monthly": args.min_monthly,
                    "min_profit_factor": args.min_profit_factor,
                    "max_profit_factor": args.max_profit_factor,
                    "prop_firm_limit_pct": args.prop_firm_limit,
                },
                "asset": asset_cfg,
                "matched_on_attempt": attempt,
                "matched_strategy": winner,
                "attempts": attempts,
            }
            out_path = _write_report(asset_key, report)
            print("")
            print("Match found:")
            print(f"  strategy: {winner.get('name')}")
            print(f"  monthly:  {winner.get('projected_monthly_pnl_pct', 0.0) * 100:.2f}%")
            print(f"  PF:       {float(winner.get('profit_factor', 0.0) or 0.0):.2f}")
            print(f"  MC p95DD: {float(winner.get('mc_p95_dd', 0.0) or 0.0) * 100:.2f}%")
            print(f"  report:   {out_path}")
            return 0

    report = {
        "status": "no_match",
        "criteria": {
            "min_monthly": args.min_monthly,
            "min_profit_factor": args.min_profit_factor,
            "max_profit_factor": args.max_profit_factor,
            "prop_firm_limit_pct": args.prop_firm_limit,
        },
        "asset": asset_cfg,
        "best_candidate_seen": best_seen,
        "attempts": attempts,
    }
    out_path = _write_report(asset_key, report)
    print("")
    print("No exact match found within the attempt limit.")
    if best_seen:
        print(f"  best near-miss: {best_seen.get('name')}")
        print(f"  monthly:        {best_seen.get('projected_monthly_pnl_pct', 0.0) * 100:.2f}%")
        print(f"  PF:             {float(best_seen.get('profit_factor', 0.0) or 0.0):.2f}")
        print(f"  MC p95DD:       {float(best_seen.get('mc_p95_dd', 0.0) or 0.0) * 100:.2f}%")
    print(f"  report:         {out_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
