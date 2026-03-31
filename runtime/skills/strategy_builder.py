"""
Strategy Builder — generates structured trading strategy schemas using local LLM.
Self-contained within Z_Claw — no external agent-network dependency.
Produces schemas compatible with _resolve_from_schema() in virtual_account.py.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from runtime.config import SKILL_MODELS, OLLAMA_HOST, STATE_DIR, ROOT
from runtime.ollama_client import chat_json, is_available

log = logging.getLogger(__name__)

MODEL = SKILL_MODELS["strategy-builder"]
ASSETS_FILE = ROOT / "divisions" / "trading" / "assets.json"

# ── Supported schema vocabulary ───────────────────────────────────────────────

INDICATOR_TYPES = [
    "bollinger_bands",
    "ema_crossover",
    "ema_above_price",
    "rsi",
    "rsi_divergence",
    "macd",
    "stochastic",
    "atr_expansion",
    "vwap",
]

CONFIRMATION_TYPES = [
    # Standard confirmations
    "atr_expansion",
    "rsi",
    "macd",
    "volume_above_avg",
    "ema_crossover",
    "stochastic",
    # Intermarket confirmations
    "intermarket_correlation",
    "intermarket_divergence",
    "intermarket_lead_lag",
]

ENTRY_TRIGGERS = [
    "indicator_cross",
    "price_touch_band",
    "price_breakout",
    "momentum_shift",
    "pullback_to_level",
    "candle_pattern",
]

EXIT_TRIGGERS = [
    "opposite_signal",
    "target_r_multiple",
    "trailing_stop",
    "time_exit",
    "band_touch",
    "indicator_reversal",
]

TIMEFRAMES = ["1m", "5m", "15m", "1h", "4h", "1d"]
DIRECTIONS = ["long", "short", "both"]
SESSIONS = ["rth", "extended", "london", "all"]
ALLOWED_SESSIONS = ["ny_rth", "london", "asia"]

REQUIRED_STRATEGY_FIELDS = {
    "id", "name", "primary_indicator", "confirmation",
    "entry", "exit", "stop_loss", "metadata",
}

REQUIRED_METADATA_FIELDS = {"timeframe", "direction"}


# ── Asset loader ──────────────────────────────────────────────────────────────

def _load_instruments() -> list[dict]:
    """Load instrument definitions from assets.json."""
    try:
        with open(ASSETS_FILE, encoding="utf-8") as f:
            return json.load(f).get("instruments", [])
    except Exception as e:
        log.warning("Could not load assets.json (%s) — using fallback", e)
        return [
            {"name": "SPX500", "ticker": "^GSPC", "futures": "MES"},
            {"name": "NAS100", "ticker": "^IXIC", "futures": "MNQ"},
            {"name": "XAUUSD", "ticker": "GC=F", "futures": "MGC"},
            {"name": "US30", "ticker": "^DJI", "futures": "MYM"},
        ]


# ── LLM prompt builder ───────────────────────────────────────────────────────

def _build_prompt(
    num_strategies: int,
    timeframe: str,
    feedback: Optional[dict],
    retired_patterns: Optional[list],
) -> list[dict]:
    """Build the system + user messages for strategy generation."""
    instruments = _load_instruments()
    inst_names = ", ".join(f"{i['name']}/{i['futures']}" for i in instruments)

    system_msg = (
        "You are a quantitative trading strategy designer for a prop firm trading system. "
        "You generate structured JSON strategy schemas that are machine-parseable. "
        "Target: prop firm compliant strategies with max 8% Monte Carlo drawdown and "
        "profit factor between 2.0 and 4.0.\n\n"
        "Focus on DAYTRADING strategies using intraday timeframes (5m primary, 1m entry, 15m confirmation). "
        "Every strategy MUST include entry_timeframe for multi-timeframe confirmation.\n\n"
        "Available instruments: " + inst_names + "\n\n"
        "IMPORTANT: Respond with ONLY a JSON object. No markdown, no explanation."
    )

    user_content = (
        f"Generate exactly {num_strategies} diverse trading strategies as JSON.\n\n"
        f"Schema requirements for each strategy:\n"
        f"- id: unique string (e.g. 'strat_bb_rsi_1h_001')\n"
        f"- name: descriptive human-readable name\n"
        f"- primary_indicator: one of {json.dumps(INDICATOR_TYPES)}\n"
        f"- confirmation: object with 'type' (one of {json.dumps(CONFIRMATION_TYPES)}) "
        f"and 'params' (dict of indicator-specific settings)\n"
        f"- entry: object with 'trigger' (one of {json.dumps(ENTRY_TRIGGERS)}) "
        f"and 'conditions' (list of condition strings)\n"
        f"- exit: object with 'trigger' (one of {json.dumps(EXIT_TRIGGERS)}), "
        f"'target_r' (float, e.g. 2.0-4.0), and 'conditions' (list)\n"
        f"- stop_loss: object with 'type' ('atr_based'|'fixed_pct'|'structure'), "
        f"'atr_multiple' (float, e.g. 1.5-3.0), 'max_pct' (float, e.g. 0.02)\n"
        f"- metadata: object with:\n"
        f"  - timeframe: primary timeframe, one of {json.dumps(TIMEFRAMES)} (default '{timeframe}')\n"
        f"  - entry_timeframe: lower timeframe for entry timing (multi-timeframe, REQUIRED)\n"
        f"    Example: timeframe='5m' for signals, entry_timeframe='1m' for precise entry\n"
        f"  - confirmation_timeframe: (optional) higher timeframe for trend filter (e.g. '15m')\n"
        f"  - direction: one of {json.dumps(DIRECTIONS)}\n"
        f"  - session: one of {json.dumps(SESSIONS)} (when to fetch data)\n"
        f"  - allowed_sessions: (optional) list from {json.dumps(ALLOWED_SESSIONS)} "
        f"(time-of-day filter for entries)\n"
        f"  - allowed_hours: (optional) list of UTC hours when entries are allowed, e.g. [14,15,16,17]\n"
        f"  - blocked_hours: (optional) list of UTC hours when entries are blocked, e.g. [0,1,2,3,4,5]\n\n"
        f"Design constraints:\n"
        f"- Max 8% Monte Carlo drawdown\n"
        f"- Profit factor target: 2.0-4.0\n"
        f"- ALL strategies should use multi-timeframe (5m signals + 1m entry + 15m trend filter preferred)\n"
        f"- At least 1 strategy should use an intermarket confirmation\n"
        f"- Diverse indicator mix — avoid duplicating primary_indicator types\n\n"
        f"Respond with: {{\"strategies\": [...]}}"
    )

    # Inject feedback from previous search cycles
    if feedback:
        feedback_lines = []
        if feedback.get("best_indicators"):
            feedback_lines.append(
                f"Best-performing indicator types from recent winners: "
                f"{', '.join(feedback['best_indicators'])}"
            )
        if feedback.get("avg_r_multiple") is not None:
            feedback_lines.append(
                f"Average R-multiple from recent trades: {feedback['avg_r_multiple']:.2f}"
            )
        if feedback.get("worst_indicators"):
            feedback_lines.append(
                f"Worst-performing indicators to de-emphasize: "
                f"{', '.join(feedback['worst_indicators'])}"
            )
        if feedback.get("notes"):
            feedback_lines.append(f"Additional notes: {feedback['notes']}")
        if feedback_lines:
            user_content += "\n\nFeedback from previous cycles:\n" + "\n".join(
                f"- {line}" for line in feedback_lines
            )

    if retired_patterns:
        user_content += (
            f"\n\nRetired patterns to AVOID (these underperformed):\n"
            + "\n".join(f"- {p}" for p in retired_patterns[:10])
        )

    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_content},
    ]


# ── Strategy validation ──────────────────────────────────────────────────────

def _validate_strategy(strat: dict) -> bool:
    """Return True if strategy has all required fields with valid values."""
    if not isinstance(strat, dict):
        return False

    # Check required top-level fields
    missing = REQUIRED_STRATEGY_FIELDS - set(strat.keys())
    if missing:
        log.debug("Strategy missing fields: %s", missing)
        return False

    # Validate primary_indicator
    if strat.get("primary_indicator") not in INDICATOR_TYPES:
        log.debug("Invalid primary_indicator: %s", strat.get("primary_indicator"))
        return False

    # Validate metadata has required fields
    meta = strat.get("metadata", {})
    if not isinstance(meta, dict):
        return False
    missing_meta = REQUIRED_METADATA_FIELDS - set(meta.keys())
    if missing_meta:
        log.debug("Strategy metadata missing fields: %s", missing_meta)
        return False

    # Validate timeframe
    if meta.get("timeframe") not in TIMEFRAMES:
        log.debug("Invalid timeframe: %s", meta.get("timeframe"))
        return False

    # Validate direction
    if meta.get("direction") not in DIRECTIONS:
        log.debug("Invalid direction: %s", meta.get("direction"))
        return False

    # Validate confirmation is a dict with a type
    conf = strat.get("confirmation", {})
    if not isinstance(conf, dict) or "type" not in conf:
        log.debug("Invalid confirmation structure")
        return False

    # Validate entry and exit are dicts
    if not isinstance(strat.get("entry"), dict):
        return False
    if not isinstance(strat.get("exit"), dict):
        return False

    # Validate stop_loss is a dict
    if not isinstance(strat.get("stop_loss"), dict):
        return False

    return True


def _ensure_id(strat: dict) -> dict:
    """Ensure strategy has a unique ID."""
    if not strat.get("id"):
        indicator = strat.get("primary_indicator", "unknown")
        tf = strat.get("metadata", {}).get("timeframe", "1h")
        strat["id"] = f"strat_{indicator}_{tf}_{uuid.uuid4().hex[:6]}"
    return strat


# ── Template-based fallback strategies ────────────────────────────────────────

def _generate_fallback_strategies() -> list[dict]:
    """
    Generate 5 preset template strategies as fallback when Ollama is unavailable.
    Covers different indicator types, includes multi-timeframe and intermarket.
    """
    return [
        {
            "id": "strat_bb_atr_1h_tpl01",
            "name": "Bollinger Band Squeeze Reversal — Hourly/15m Entry",
            "primary_indicator": "bollinger_bands",
            "confirmation": {
                "type": "atr_expansion",
                "params": {"atr_period": 14, "lookback": 5},
            },
            "entry": {
                "trigger": "price_touch_band",
                "conditions": [
                    "price touches lower Bollinger band on 1h",
                    "ATR expanding above 5-bar average",
                    "15m close above lower band confirms entry",
                ],
            },
            "exit": {
                "trigger": "band_touch",
                "target_r": 2.5,
                "conditions": [
                    "price reaches middle Bollinger band",
                    "or target R-multiple hit",
                ],
            },
            "stop_loss": {
                "type": "atr_based",
                "atr_multiple": 2.0,
                "max_pct": 0.02,
            },
            "metadata": {
                "timeframe": "1h",
                "entry_timeframe": "15m",
                "direction": "long",
                "session": "rth",
                "allowed_sessions": ["ny_rth"],
            },
        },
        {
            "id": "strat_ema_rsi_4h_tpl02",
            "name": "EMA Crossover with RSI Confirmation — 4H",
            "primary_indicator": "ema_crossover",
            "confirmation": {
                "type": "rsi",
                "params": {"period": 14, "oversold": 30, "overbought": 70},
            },
            "entry": {
                "trigger": "indicator_cross",
                "conditions": [
                    "EMA20 crosses above EMA50",
                    "RSI between 30-60 (not overbought)",
                ],
            },
            "exit": {
                "trigger": "indicator_reversal",
                "target_r": 3.0,
                "conditions": [
                    "EMA20 crosses below EMA50",
                    "or RSI above 75",
                    "or target R-multiple hit",
                ],
            },
            "stop_loss": {
                "type": "atr_based",
                "atr_multiple": 1.5,
                "max_pct": 0.015,
            },
            "metadata": {
                "timeframe": "4h",
                "entry_timeframe": "15m",
                "direction": "both",
                "session": "rth",
                "allowed_sessions": ["ny_rth", "london"],
            },
        },
        {
            "id": "strat_macd_vol_1h_tpl03",
            "name": "MACD Momentum with Volume — Hourly",
            "primary_indicator": "macd",
            "confirmation": {
                "type": "volume_above_avg",
                "params": {"volume_ma_period": 20, "threshold_mult": 1.3},
            },
            "entry": {
                "trigger": "momentum_shift",
                "conditions": [
                    "MACD crosses above signal line",
                    "MACD histogram increasing",
                    "volume above 1.3x 20-bar average",
                ],
            },
            "exit": {
                "trigger": "opposite_signal",
                "target_r": 2.0,
                "conditions": [
                    "MACD crosses below signal line",
                    "or target R-multiple hit",
                ],
            },
            "stop_loss": {
                "type": "atr_based",
                "atr_multiple": 2.0,
                "max_pct": 0.012,
            },
            "metadata": {
                "timeframe": "1h",
                "direction": "both",
                "session": "rth",
                "allowed_sessions": ["ny_rth", "london"],
                "allowed_hours": [8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
            },
        },
        {
            # Multi-timeframe: 1h direction + 15m entry
            "id": "strat_ema_stoch_mtf_tpl04",
            "name": "Hourly EMA Direction + 15m Stochastic Entry (Multi-TF)",
            "primary_indicator": "ema_above_price",
            "confirmation": {
                "type": "stochastic",
                "params": {"k_period": 14, "d_period": 3, "oversold": 20, "overbought": 80},
            },
            "entry": {
                "trigger": "pullback_to_level",
                "conditions": [
                    "1h: price above EMA50 (bullish bias)",
                    "15m: stochastic %K crosses above %D from below 20",
                    "15m: price pulls back to EMA20 on entry timeframe",
                ],
            },
            "exit": {
                "trigger": "target_r_multiple",
                "target_r": 3.0,
                "conditions": [
                    "stochastic %K above 80 on 15m",
                    "or 1h EMA direction reversal",
                    "or target R-multiple hit",
                ],
            },
            "stop_loss": {
                "type": "structure",
                "atr_multiple": 2.5,
                "max_pct": 0.02,
            },
            "metadata": {
                "timeframe": "1h",
                "entry_timeframe": "15m",
                "direction": "long",
                "session": "rth",
                "allowed_sessions": ["ny_rth"],
                "blocked_hours": [0, 1, 2, 3, 4, 5, 6, 7],
            },
        },
        {
            # Intermarket strategy
            "id": "strat_rsi_intermarket_1h_tpl05",
            "name": "RSI with Gold-SPX Intermarket Divergence — Hourly",
            "primary_indicator": "rsi",
            "confirmation": {
                "type": "intermarket_divergence",
                "params": {
                    "reference_instrument": "XAUUSD",
                    "target_instrument": "SPX500",
                    "divergence_lookback": 10,
                    "correlation_threshold": -0.3,
                },
            },
            "entry": {
                "trigger": "momentum_shift",
                "conditions": [
                    "RSI crosses above 30 from oversold on 1h",
                    "XAUUSD showing strength (rising) while SPX500 oversold",
                    "intermarket divergence detected in last 10 bars",
                    "15m confirms momentum shift with rising close",
                ],
            },
            "exit": {
                "trigger": "target_r_multiple",
                "target_r": 2.5,
                "conditions": [
                    "RSI above 65",
                    "or intermarket convergence resumes",
                    "or target R-multiple hit",
                ],
            },
            "stop_loss": {
                "type": "atr_based",
                "atr_multiple": 2.0,
                "max_pct": 0.025,
            },
            "metadata": {
                "timeframe": "1h",
                "entry_timeframe": "15m",
                "direction": "long",
                "session": "rth",
                "allowed_sessions": ["ny_rth", "london"],
            },
        },
    ]


# ── Main entry point ─────────────────────────────────────────────────────────

def run(
    num_strategies: int = 5,
    timeframe: str = "5m",
    feedback: Optional[dict] = None,
    retired_patterns: Optional[list] = None,
) -> dict:
    """
    Generate trading strategy schemas using local Ollama LLM with template fallback.

    Args:
        num_strategies: Number of strategies to generate (default 5).
        timeframe: Default primary timeframe for strategies.
        feedback: Optional dict with keys: best_indicators, avg_r_multiple,
                  worst_indicators, notes — from previous search cycles.
        retired_patterns: Optional list of pattern descriptions to avoid.

    Returns:
        dict with status, strategies, summary, metrics, escalate, escalation_reason.
    """
    generated = []
    discarded = 0
    llm_used = False
    llm_count = 0
    template_count = 0
    errors = []

    # ── Step 1: Try LLM generation via local Ollama ──────────────────────────
    if is_available(MODEL):
        try:
            messages = _build_prompt(num_strategies, timeframe, feedback, retired_patterns)
            # task_type="strategy-builder" passed in comment for training data capture;
            # chat_json does not currently accept task_type — when the parameter is
            # added to ollama_client.chat_json, uncomment it here.
            raw = chat_json(
                MODEL,
                messages,
                temperature=0.7,
                max_tokens=2000,
            )
            llm_used = True

            # Parse response — expect {"strategies": [...]}
            strategies_raw = []
            if isinstance(raw, dict) and "strategies" in raw:
                strategies_raw = raw["strategies"]
            elif isinstance(raw, list):
                strategies_raw = raw
            elif isinstance(raw, dict):
                # Model might have returned a single strategy
                strategies_raw = [raw]

            if not isinstance(strategies_raw, list):
                strategies_raw = []

            for strat in strategies_raw:
                strat = _ensure_id(strat)
                if _validate_strategy(strat):
                    generated.append(strat)
                    llm_count += 1
                else:
                    discarded += 1
                    log.debug("Discarded malformed strategy: %s", strat.get("name", "?"))

            log.info(
                "LLM generated %d valid strategies (%d discarded)",
                llm_count, discarded,
            )

        except Exception as e:
            errors.append(f"LLM generation failed: {e}")
            log.warning("Strategy builder LLM call failed: %s", e)
    else:
        log.info("Ollama model %s not available — using template fallback", MODEL)

    # ── Step 2: Fill remaining with template fallback ────────────────────────
    if len(generated) < num_strategies:
        needed = num_strategies - len(generated)
        templates = _generate_fallback_strategies()

        # Avoid duplicating indicator types already in LLM results
        existing_indicators = {s["primary_indicator"] for s in generated}
        for tmpl in templates:
            if needed <= 0:
                break
            if tmpl["primary_indicator"] not in existing_indicators:
                generated.append(tmpl)
                template_count += 1
                existing_indicators.add(tmpl["primary_indicator"])
                needed -= 1

        # If still not enough, add remaining templates regardless of indicator overlap
        for tmpl in templates:
            if needed <= 0:
                break
            if tmpl["id"] not in {s["id"] for s in generated}:
                generated.append(tmpl)
                template_count += 1
                needed -= 1

    # ── Step 3: Persist generated strategies to state ────────────────────────
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    output_file = STATE_DIR / "generated_strategies.json"
    try:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "llm_model": MODEL if llm_used else None,
            "strategy_count": len(generated),
            "strategies": generated,
        }
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        log.info("Saved %d strategies to %s", len(generated), output_file)
    except Exception as e:
        errors.append(f"Failed to save strategies: {e}")
        log.error("Could not write generated_strategies.json: %s", e)

    # ── Step 4: Build result ─────────────────────────────────────────────────
    total = len(generated)

    if total == 0:
        status = "failed"
    elif llm_used and discarded > 0 and llm_count < num_strategies:
        status = "partial"
    else:
        status = "success"

    source_parts = []
    if llm_count:
        source_parts.append(f"{llm_count} LLM")
    if template_count:
        source_parts.append(f"{template_count} template fallback")
    source_desc = ", ".join(source_parts) if source_parts else "none"

    summary = f"Generated {total} strategies ({source_desc})"
    if discarded:
        summary += f", {discarded} discarded"

    return {
        "status": status,
        "strategies": generated,
        "summary": summary,
        "metrics": {
            "generated": total,
            "valid": total,
            "discarded": discarded,
            "llm_used": llm_used,
            "llm_count": llm_count,
            "template_count": template_count,
        },
        "escalate": False,
        "escalation_reason": "",
    }
