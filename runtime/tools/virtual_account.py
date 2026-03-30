"""
Virtual account manager — SPX500 and Gold paper trading with real yfinance data.
No broker, no KYC. Uses real market prices to simulate trade execution.
Reads agent-network cycle state for active strategy. Writes virtual_account.json.
"""

import json
import logging
import statistics
import uuid
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

AGENT_NETWORK_STATE = Path("C:/Users/Tyler/agent-network/state")
VIRTUAL_ACCT_PATH   = AGENT_NETWORK_STATE / "virtual_account.json"
ASSETS_FILE         = Path("divisions/trading/assets.json")


def _load_instruments() -> dict[str, str]:
    """Load name->ticker mapping from assets.json. Falls back to SPX500+XAUUSD."""
    try:
        with open(ASSETS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return {inst["name"]: inst["ticker"] for inst in data.get("instruments", [])}
    except Exception as e:
        log.warning("Could not load assets.json (%s) — using fallback instruments", e)
        return {"SPX500": "^GSPC", "XAUUSD": "GC=F", "NAS100": "^IXIC", "US30": "^DJI"}


INSTRUMENTS = _load_instruments()

# Timeframe -> (yfinance interval, yfinance period)
_TF_MAP = {
    "15m": ("15m", "5d"),    # ~130 intraday 15-min bars
    "1h":  ("1h",  "30d"),   # ~480 1h bars
    "4h":  ("1h",  "30d"),   # fetch 1h, resample → ~120 4h bars
    "1d":  ("1d",  "3mo"),   # daily fallback
}

DEFAULT_BALANCE    = 10_000.0
RISK_PER_TRADE_PCT = 1.0   # 1% of account per trade
STOP_PCT           = 0.01  # 1% stop loss distance
SLIPPAGE_BPS       = 5     # default fallback
DAILY_LOSS_HALT_PCT = 3.0  # halt if daily PnL < -3% of account
STREAK_HALT_COUNT   = 5    # halt after N consecutive losses
TRAILING_DD_PCT     = 5.0  # trailing drawdown limit (Apex $50K = 5%)
MAX_CONTRACTS_PER_INSTRUMENT = 5   # Topstep $50K default
MAX_TOTAL_CONTRACTS = 10           # Total across all instruments

# Per-instrument slippage (bps)
INSTRUMENT_SLIPPAGE_BPS = {
    "SPX500": 3,
    "XAUUSD": 8,
    "CRUDE":  5,
    "BONDS":  3,
    "NAS100": 4,
    "US30":   3,
}

# Diversified pairwise correlations — all below 0.80 threshold
INSTRUMENT_CORRELATIONS = {
    ("SPX500", "XAUUSD"): -0.15,
    ("SPX500", "CRUDE"):   0.40,
    ("SPX500", "BONDS"):  -0.30,
    ("SPX500", "NAS100"):  0.92,
    ("SPX500", "US30"):    0.95,
    ("XAUUSD", "CRUDE"):   0.25,
    ("XAUUSD", "BONDS"):   0.20,
    ("CRUDE",  "BONDS"):  -0.15,
    ("NAS100", "US30"):    0.90,
    ("NAS100", "XAUUSD"): -0.10,
    ("NAS100", "CRUDE"):   0.35,
    ("NAS100", "BONDS"):  -0.25,
    ("US30",   "XAUUSD"): -0.12,
    ("US30",   "CRUDE"):   0.38,
    ("US30",   "BONDS"):  -0.28,
}
MAX_PORTFOLIO_CORRELATION = 0.80

# ── Time-of-day session definitions ───────────────────────────────────────────

SESSION_HOURS = {
    "ny_rth":      list(range(10, 16)),                          # 10:00-15:59 ET
    "ny_extended":  list(range(8, 18)),                           # 8:00-17:59 ET
    "london":      list(range(3, 12)),                           # 3:00-11:59 ET
    "asia":        list(range(19, 24)) + list(range(0, 4)),      # 19:00-3:59 ET
    "all":         list(range(0, 24)),
}


def _check_time_filter(metadata: dict) -> bool:
    """
    Check time-of-day filters from strategy schema metadata.

    Returns True if the current time is allowed (trading should proceed),
    False if the current time is blocked (instrument should be skipped).

    Metadata keys examined:
        allowed_hours    - explicit list of ET hours, e.g. [10, 11, 12, 13, 14, 15]
        blocked_hours    - explicit list of ET hours to skip, e.g. [9, 16]
        allowed_sessions - list of named sessions, e.g. ["ny_rth"]
    """
    allowed_hours    = metadata.get("allowed_hours")       # e.g., [10, 11, 12, 13, 14, 15]
    blocked_hours    = metadata.get("blocked_hours")       # e.g., [9, 16]
    allowed_sessions = metadata.get("allowed_sessions")    # e.g., ["ny_rth"]

    # Nothing configured -> no filtering
    if not allowed_hours and not blocked_hours and not allowed_sessions:
        return True

    # Simple ET approximation: ET = UTC - 4 (EDT) or UTC - 5 (EST).
    # Use -4 as default -- most of the year is EDT.
    current_hour = datetime.now(timezone.utc).hour
    et_hour = (current_hour - 4) % 24

    time_allowed = True

    if allowed_sessions:
        session_hours: set[int] = set()
        for session_name in allowed_sessions:
            session_hours.update(SESSION_HOURS.get(session_name, []))
        if et_hour not in session_hours:
            time_allowed = False

    if allowed_hours and et_hour not in allowed_hours:
        time_allowed = False

    if blocked_hours and et_hour in blocked_hours:
        time_allowed = False

    return time_allowed


def _correlation(inst_a: str, inst_b: str) -> float:
    key = (inst_a, inst_b) if (inst_a, inst_b) in INSTRUMENT_CORRELATIONS else (inst_b, inst_a)
    return INSTRUMENT_CORRELATIONS.get(key, 0.0)


def _slippage_bps(instrument: str) -> int:
    """Per-instrument slippage in basis points."""
    return INSTRUMENT_SLIPPAGE_BPS.get(instrument, SLIPPAGE_BPS)


def _load_file(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load %s: %s", path, e)
        return None


def _save_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def load_virtual_account() -> dict:
    """Load or initialize virtual account state."""
    data = _load_file(VIRTUAL_ACCT_PATH)
    if data:
        return data
    return {
        "account_balance":    DEFAULT_BALANCE,
        "initial_balance":    DEFAULT_BALANCE,
        "risk_per_trade_pct": RISK_PER_TRADE_PCT,
        "instruments":        INSTRUMENTS,
        "open_positions":     [],
        "trade_log":          [],
        # Item 35 — empirical fill-tracking log; each entry records expected vs
        # actual fill price and the observed slippage in basis points.
        "fill_tracking":      [],
        "updated_at":         datetime.now(timezone.utc).isoformat(),
    }


def save_virtual_account(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save_file(VIRTUAL_ACCT_PATH, data)
    log.info("Virtual account saved: balance=%.2f open=%d",
             data.get("account_balance", 0), len(data.get("open_positions", [])))


# ── Price data ─────────────────────────────────────────────────────────────────

def _resample_4h(ohlcv: dict) -> dict:
    """Aggregate 1h OHLCV dict into 4h candles (every 4 bars)."""
    dates, opens, highs = ohlcv["date"], ohlcv["open"], ohlcv["high"]
    lows, closes, volumes = ohlcv["low"], ohlcv["close"], ohlcv["volume"]
    n = len(dates)
    r_d, r_o, r_h, r_l, r_c, r_v = [], [], [], [], [], []
    i = 0
    while i < n:
        end = min(i + 4, n)
        r_d.append(dates[i])
        r_o.append(opens[i])
        r_h.append(max(highs[i:end]))
        r_l.append(min(lows[i:end]))
        r_c.append(closes[end - 1])
        r_v.append(sum(volumes[i:end]))
        i += 4
    return {"ticker": ohlcv["ticker"], "date": r_d, "open": r_o,
            "high": r_h, "low": r_l, "close": r_c, "volume": r_v}


def fetch_ohlcv(ticker: str, timeframe: str = "1d", session: str = "all") -> Optional[dict]:
    """
    Fetch OHLCV via best available market data provider.
    Falls back to yfinance if the provider module is unavailable or fails.

    4h is fetched as 1h then resampled. Returns dict with lists:
    date, open, high, low, close, volume.

    Args:
        ticker:    yfinance ticker symbol (e.g. "^GSPC", "GC=F").
        timeframe: bar size — one of "15m", "1h", "4h", "1d".
        session:   trading-session filter applied to intraday timeframes.
                   "all"      — no filtering (default, backward-compatible).
                   "rth"      — Regular Trading Hours 9:30-16:00 ET (14:30-21:00 UTC).
                   "extended" — Extended hours 8:00-17:00 ET (13:00-22:00 UTC).
                   "london"   — London session 08:00-16:30 UTC.
                   Ignored for daily ("1d") timeframe.
    """
    # Try the new provider abstraction first
    try:
        from providers.market_data import get_provider
        provider = get_provider()
        result = provider.fetch_ohlcv(ticker, timeframe=timeframe, session=session)
        if result:
            return result
    except ImportError:
        pass  # providers module not available, fall through to yfinance
    except Exception as e:
        log.warning("Market data provider failed, falling back to yfinance: %s", e)

    # Existing yfinance code as fallback
    yf_interval, yf_period = _TF_MAP.get(timeframe, ("1d", "3mo"))
    try:
        import yfinance as yf
        df = yf.download(ticker, period=yf_period, interval=yf_interval,
                         auto_adjust=False, progress=False)
        if df.empty:
            log.warning("No data returned for %s", ticker)
            return None
        # Flatten in case yfinance returns MultiIndex columns
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)

        # Session filtering for intraday timeframes
        if session != "all" and timeframe in ("15m", "1h", "4h"):
            SESSION_RANGES = {
                "rth":      (14, 30, 21, 0),   # 9:30-16:00 ET -> 14:30-21:00 UTC
                "extended": (13, 0, 22, 0),     # 8:00-17:00 ET -> 13:00-22:00 UTC
                "london":   (8, 0, 16, 30),     # 08:00-16:30 UTC
            }
            if session in SESSION_RANGES:
                start_h, start_m, end_h, end_m = SESSION_RANGES[session]
                if hasattr(df.index, 'hour'):
                    mask = (
                        (df.index.hour > start_h)
                        | ((df.index.hour == start_h) & (df.index.minute >= start_m))
                    ) & (
                        (df.index.hour < end_h)
                        | ((df.index.hour == end_h) & (df.index.minute < end_m))
                    )
                    df = df[mask]
                    if df.empty:
                        log.warning("No bars after session filter '%s' for %s", session, ticker)
                        return None

        ohlcv = {
            "ticker": ticker,
            "date":   [str(d) for d in df.index],
            "open":   [float(v) for v in df["Open"].tolist()],
            "high":   [float(v) for v in df["High"].tolist()],
            "low":    [float(v) for v in df["Low"].tolist()],
            "close":  [float(v) for v in df["Close"].tolist()],
            "volume": [float(v) for v in df["Volume"].tolist()],
        }
        if timeframe == "4h":
            ohlcv = _resample_4h(ohlcv)
        log.debug("Fetched %d %s bars for %s (session=%s)", len(ohlcv["close"]), timeframe, ticker, session)
        return ohlcv
    except ImportError:
        log.error("yfinance not installed — run: pip install yfinance pandas")
        return None
    except Exception as e:
        log.error("yfinance fetch failed for %s: %s", ticker, e)
        return None


# ── Indicator calculations (pure Python, no numpy) ────────────────────────────

def _calc_ema(prices: list, period: int) -> list:
    if len(prices) < period:
        return [None] * len(prices)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    ema = sum(prices[:period]) / period
    result.append(ema)
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
        result.append(ema)
    return result


def _calc_atr(high: list, low: list, close: list, period: int = 14) -> list:
    tr_list = []
    for i in range(len(close)):
        if i == 0:
            tr_list.append(high[i] - low[i])
        else:
            tr = max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
            tr_list.append(tr)
    return _calc_ema(tr_list, period)


def _calc_bollinger(prices: list, period: int = 20,
                    std_mult: float = 2.0) -> tuple[list, list, list]:
    upper, middle, lower = [], [], []
    for i in range(len(prices)):
        if i < period - 1:
            upper.append(None)
            middle.append(None)
            lower.append(None)
        else:
            window = prices[i - period + 1 : i + 1]
            mean   = sum(window) / period
            std    = statistics.stdev(window)
            upper.append(mean + std_mult * std)
            middle.append(mean)
            lower.append(mean - std_mult * std)
    return upper, middle, lower


def _atr_expanding(atr: list, lookback: int = 5) -> Optional[bool]:
    valid = [v for v in atr if v is not None]
    if len(valid) < lookback + 1:
        return None
    current    = valid[-1]
    recent_avg = sum(valid[-(lookback + 1) : -1]) / lookback
    return current > recent_avg


def _last(values: list) -> Optional[float]:
    return next((v for v in reversed(values) if v is not None), None)


def _calc_rsi(prices: list, period: int = 14) -> Optional[float]:
    """Calculate RSI for the last bar. Returns None if insufficient data."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(delta if delta > 0 else 0)
        losses.append(-delta if delta < 0 else 0)
    if len(gains) < period:
        return None
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── Signal engine ──────────────────────────────────────────────────────────────

SIGNAL_WEIGHTS = {
    "trend":        0.30,
    "momentum":     0.25,
    "volatility":   0.20,
    "volume":       0.00,
    "structure":    0.10,
    "intermarket":  0.15,
}


def _score_intermarket(close: list, intermarket_data: dict, instrument_name: str) -> float:
    """Score intermarket alignment 0.0-1.0. 0.5 = neutral."""
    if not intermarket_data or not close or len(close) < 10:
        return 0.5
    primary_ret = (close[-1] - close[-5]) / close[-5] if len(close) >= 5 else 0
    alignment_scores = []
    for ref_name, ref_ohlcv in intermarket_data.items():
        if ref_name == instrument_name or not ref_ohlcv or not ref_ohlcv.get("close"):
            continue
        ref_close = ref_ohlcv["close"]
        if len(ref_close) < 5:
            continue
        ref_ret = (ref_close[-1] - ref_close[-5]) / ref_close[-5]
        expected_corr = _correlation(instrument_name, ref_name)
        if expected_corr > 0.1:
            aligned = (primary_ret > 0 and ref_ret > 0) or (primary_ret < 0 and ref_ret < 0)
        elif expected_corr < -0.1:
            aligned = (primary_ret > 0 and ref_ret < 0) or (primary_ret < 0 and ref_ret > 0)
        else:
            continue
        alignment_scores.append(0.75 if aligned else 0.25)
    return sum(alignment_scores) / len(alignment_scores) if alignment_scores else 0.5


def _composite_score(close: list, high: list, low: list,
                     intermarket_data: dict = None, instrument_name: str = "") -> dict:
    """
    Compute a weighted composite score from multiple signal categories.
    Each sub-score is 0.0–1.0; the composite is their weighted average.
    """
    scores = {}

    # Trend: EMA20 vs EMA50
    ema20_val = _last(_calc_ema(close, 20))
    ema50_val = _last(_calc_ema(close, 50))
    if ema20_val is not None and ema50_val is not None and ema50_val != 0:
        trend_ratio = ema20_val / ema50_val
        scores["trend"] = max(0.0, min(1.0, 0.5 + (trend_ratio - 1.0) * 10))
    else:
        scores["trend"] = 0.5

    # Momentum: RSI
    rsi = _calc_rsi(close)
    if rsi is not None:
        scores["momentum"] = rsi / 100.0
    else:
        scores["momentum"] = 0.5

    # Volatility: ATR expansion
    atr = _calc_atr(high, low, close)
    expanding = _atr_expanding(atr)
    if expanding is True:
        scores["volatility"] = 0.7
    elif expanding is False:
        scores["volatility"] = 0.3
    else:
        scores["volatility"] = 0.5

    # Volume: placeholder (no volume signal currently)
    scores["volume"] = 0.5

    # Structure: price vs Bollinger bands
    bb_upper, bb_mid, bb_lower = _calc_bollinger(close)
    bb_u = _last(bb_upper)
    bb_l = _last(bb_lower)
    if bb_u is not None and bb_l is not None and bb_u != bb_l:
        scores["structure"] = max(0.0, min(1.0, (close[-1] - bb_l) / (bb_u - bb_l)))
    else:
        scores["structure"] = 0.5

    # Intermarket
    scores["intermarket"] = _score_intermarket(close, intermarket_data, instrument_name)

    # Weighted composite
    composite = sum(scores[k] * SIGNAL_WEIGHTS.get(k, 0) for k in scores)
    total_weight = sum(SIGNAL_WEIGHTS.get(k, 0) for k in scores)
    composite = composite / total_weight if total_weight > 0 else 0.5

    return {"scores": scores, "composite": round(composite, 4)}


def _resolve_from_schema(strategy_schema: dict, ohlcv: dict,
                         intermarket_data: dict = None, instrument_name: str = "",
                         entry_ohlcv: dict = None) -> dict:
    """
    Evaluate a strategy_schema's confirmation indicators against OHLCV data.
    When *entry_ohlcv* is provided (multi-timeframe mode), confirmation/entry/exit
    indicators are computed on the entry-timeframe data while the primary
    directional indicator still uses *ohlcv*.
    Returns confirmation result dict.
    """
    close = ohlcv["close"]
    high  = ohlcv["high"]
    low   = ohlcv["low"]

    # Use entry-timeframe data for confirmation if available (MTF)
    confirm_ohlcv = entry_ohlcv if entry_ohlcv else ohlcv
    confirm_close = confirm_ohlcv.get("close", close)
    confirm_high  = confirm_ohlcv.get("high", high)
    confirm_low   = confirm_ohlcv.get("low", low)

    confirmations = []
    confirm_list = []

    # Extract confirmation(s) from schema
    ci = strategy_schema.get("confirmation_indicator")
    if isinstance(ci, dict):
        confirm_list = [ci]
    elif isinstance(ci, list):
        confirm_list = ci

    direction = strategy_schema.get("direction", "long").lower()

    for confirm in confirm_list:
        confirm_type = confirm.get("type", "").lower()
        confirmation_met = True
        confirmation_desc = ""

        if confirm_type == "atr_expansion":
            atr = _calc_atr(confirm_high, confirm_low, confirm_close, confirm.get("params", {}).get("period", 14))
            expanding = _atr_expanding(atr)
            confirmation_met = expanding is True
            mtf_tag = " (MTF)" if entry_ohlcv else ""
            confirmation_desc = f"ATR {'expanding' if expanding else 'not expanding'}{mtf_tag}"

        elif confirm_type == "rsi":
            period = confirm.get("params", {}).get("period", 14)
            rsi = _calc_rsi(confirm_close, period)
            if rsi is not None:
                threshold = confirm.get("params", {}).get("threshold", 50)
                if direction == "long":
                    confirmation_met = rsi > threshold
                else:
                    confirmation_met = rsi < (100 - threshold)
                mtf_tag = " (MTF)" if entry_ohlcv else ""
                confirmation_desc = f"RSI({period})={rsi:.1f} vs {threshold}{mtf_tag}"
            else:
                confirmation_desc = "RSI insufficient data"

        elif confirm_type == "stochastic":
            # Stochastic confirmation placeholder
            confirmation_met = True
            confirmation_desc = "Stochastic (pass-through)"

        elif confirm_type == "intermarket_trend":
            ref_name = confirm.get("reference_instrument", "")
            ref_alignment = confirm.get("alignment", "same_direction")
            ref_ohlcv = intermarket_data.get(ref_name) if intermarket_data else None
            if ref_ohlcv and ref_ohlcv.get("close"):
                ref_close = ref_ohlcv["close"]
                ref_ema20 = _last(_calc_ema(ref_close, 20))
                ref_ema50 = _last(_calc_ema(ref_close, 50))
                if ref_ema20 is not None and ref_ema50 is not None:
                    ref_trending_up = ref_ema20 > ref_ema50
                    if ref_alignment == "same_direction":
                        confirmation_met = ref_trending_up == (direction == "long")
                    else:
                        confirmation_met = ref_trending_up != (direction == "long")
                    confirmation_desc = f"{ref_name} EMA20/50 {'confirms' if confirmation_met else 'conflicts'} ({ref_alignment})"

        elif confirm_type == "intermarket_momentum":
            ref_name = confirm.get("reference_instrument", "")
            ref_ohlcv = intermarket_data.get(ref_name) if intermarket_data else None
            if ref_ohlcv and ref_ohlcv.get("close"):
                ref_rsi = _calc_rsi(ref_ohlcv["close"], confirm.get("period", 14))
                if ref_rsi is not None:
                    primary_bullish = direction == "long"
                    ref_bullish = ref_rsi > 50
                    confirmation_met = primary_bullish == ref_bullish
                    confirmation_desc = f"{ref_name} RSI({ref_rsi:.0f}) {'aligns' if confirmation_met else 'diverges'}"

        elif confirm_type == "intermarket_divergence":
            ref_name = confirm.get("reference_instrument", "")
            ref_ohlcv = intermarket_data.get(ref_name) if intermarket_data else None
            if ref_ohlcv and ref_ohlcv.get("close") and len(close) >= 5 and len(ref_ohlcv["close"]) >= 5:
                primary_ret = (close[-1] - close[-5]) / close[-5]
                ref_ret = (ref_ohlcv["close"][-1] - ref_ohlcv["close"][-5]) / ref_ohlcv["close"][-5]
                expected_corr = _correlation(instrument_name, ref_name) if instrument_name else 0
                diverging = (primary_ret > 0 and ref_ret < 0) or (primary_ret < 0 and ref_ret > 0)
                confirmation_met = diverging and abs(expected_corr) > 0.2
                confirmation_desc = f"Divergence vs {ref_name}: primary {primary_ret:+.2%}, ref {ref_ret:+.2%}"

        confirmations.append({
            "type": confirm_type,
            "met": confirmation_met,
            "description": confirmation_desc,
        })

    all_met = all(c["met"] for c in confirmations) if confirmations else True
    return {"confirmations": confirmations, "all_met": all_met}


def get_strategy_signals(strategy_id: str, ohlcv: dict,
                         strategy_schema: dict = None,
                         intermarket_data: dict = None,
                         entry_ohlcv: dict = None) -> dict:
    """
    Generate entry/exit signals based on strategy_id and OHLCV data.

    When *entry_ohlcv* is provided (multi-timeframe mode), confirmation
    indicators (ATR expansion, Bollinger bands for confirmation, etc.) are
    computed on the entry-timeframe data while the primary directional
    indicator still uses *ohlcv*.

    Returns:
      {"entry": bool, "exit": bool, "side": "buy"|"sell",
       "reason": str, "current_price": float}
    """
    close = ohlcv["close"]
    high  = ohlcv["high"]
    low   = ohlcv["low"]
    sid   = strategy_id.lower()

    current_price = close[-1] if close else 0.0
    result = {
        "entry": False, "exit": False,
        "side": "buy",  "reason": "",
        "current_price": current_price,
    }

    if not close or len(close) < 40:
        result["reason"] = "Insufficient price history (<40 bars)"
        return result

    # Use entry-timeframe data for confirmation if available (MTF)
    confirm_ohlcv = entry_ohlcv if entry_ohlcv else ohlcv
    confirm_close = confirm_ohlcv.get("close", close)
    confirm_high = confirm_ohlcv.get("high", high)
    confirm_low = confirm_ohlcv.get("low", low)

    atr          = _calc_atr(confirm_high, confirm_low, confirm_close)
    atr_expanding = _atr_expanding(atr)

    # ── EMA + Price Above + ATR Expanding (Long) ──────────────────────────────
    if "ema" in sid and ("above" in sid or "pricabove" in sid or "priceabove" in sid):
        period = 38
        for p in [200, 50, 38, 21, 20]:
            if str(p) in sid:
                period = p
                break
        ema     = _calc_ema(close, period)
        ema_val = _last(ema)
        if ema_val is None:
            result["reason"] = f"EMA{period} insufficient data"
            return result
        if current_price > ema_val and atr_expanding:
            result["entry"] = True
            result["side"]  = "buy"
            result["reason"] = (
                f"Price ${current_price:.2f} above EMA{period} ${ema_val:.2f} "
                f"with expanding ATR"
            )
        elif current_price < ema_val:
            result["exit"]   = True
            result["reason"] = (
                f"Price ${current_price:.2f} below EMA{period} ${ema_val:.2f} — exit"
            )

    # ── Bollinger Lower Band Touch + ATR Expanding ────────────────────────────
    elif "bollinger" in sid or "boll" in sid:
        bb_upper, bb_mid, bb_lower = _calc_bollinger(close)
        prev_close = close[-2] if len(close) >= 2 else current_price
        prev_lower = _last(bb_lower[:-1])
        curr_lower = _last(bb_lower)
        curr_mid   = _last(bb_mid)

        if curr_lower is None:
            result["reason"] = "Bollinger insufficient data"
            return result

        bb_touch = (prev_lower is not None) and (prev_close <= prev_lower)
        if bb_touch and atr_expanding and current_price > curr_lower:
            result["entry"] = True
            result["side"]  = "buy"
            result["reason"] = (
                f"Bollinger lower touch ${curr_lower:.2f} → "
                f"rebounding ${current_price:.2f} with expanding ATR"
            )
        elif curr_mid and current_price >= curr_mid:
            result["exit"]   = True
            result["reason"] = (
                f"Price ${current_price:.2f} reached Bollinger middle "
                f"${curr_mid:.2f} — take profit"
            )

    # ── Generic EMA crossover fallback ────────────────────────────────────────
    else:
        ema20 = _calc_ema(close, 20)
        ema50 = _calc_ema(close, 50)
        e20   = _last(ema20)
        e50   = _last(ema50)
        if e20 and e50:
            if e20 > e50 and atr_expanding:
                result["entry"] = True
                result["side"]  = "buy"
                result["reason"] = (
                    f"EMA20 ${e20:.2f} above EMA50 ${e50:.2f} with expanding ATR"
                )
            elif e20 < e50:
                result["exit"]   = True
                result["reason"] = (
                    f"EMA20 ${e20:.2f} crossed below EMA50 ${e50:.2f}"
                )

    # ── Schema-based confirmation overlay ─────────────────────────────────────
    if strategy_schema and result["entry"]:
        schema_result = _resolve_from_schema(strategy_schema, ohlcv,
                                             intermarket_data=intermarket_data,
                                             instrument_name="",
                                             entry_ohlcv=entry_ohlcv)
        if not schema_result["all_met"]:
            failed = [c for c in schema_result["confirmations"] if not c["met"]]
            result["entry"] = False
            result["reason"] += " | BLOCKED by: " + ", ".join(c["description"] for c in failed)

    return result


# ── Stop loss check ────────────────────────────────────────────────────────────

def _stop_hit(position: dict, current_price: float) -> bool:
    stop = position.get("stop_loss")
    if stop is None:
        return False
    side = position.get("side", "buy")
    return current_price <= stop if side == "buy" else current_price >= stop


# ── VIX fetcher ────────────────────────────────────────────────────────────────

def _fetch_vix() -> float:
    """Fetch current VIX level. Returns 20.0 (neutral) on failure."""
    try:
        import yfinance as yf
        df = yf.download("^VIX", period="2d", interval="1d", auto_adjust=False, progress=False)
        if not df.empty:
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)
            return float(df["Close"].iloc[-1])
    except Exception:
        pass
    return 20.0


# ── Main runner ────────────────────────────────────────────────────────────────

def run_virtual_account(cycle_state: Optional[dict] = None) -> dict:
    """
    Check signals for all instruments and execute simulated trades.
    Returns summary dict consumed by virtual_trader.py skill.
    """
    account = load_virtual_account()

    # ── Trailing drawdown check ─────────────────────────────────────────────
    peak_balance = account.get("peak_balance", account.get("initial_balance", DEFAULT_BALANCE))
    current_balance = account.get("account_balance", DEFAULT_BALANCE)
    if current_balance > peak_balance:
        peak_balance = current_balance
    account["peak_balance"] = peak_balance
    trailing_floor = peak_balance * (1 - TRAILING_DD_PCT / 100) if TRAILING_DD_PCT > 0 else 0
    trailing_dd_current = round((peak_balance - current_balance) / peak_balance * 100, 2) if peak_balance > 0 else 0

    if TRAILING_DD_PCT > 0 and current_balance <= trailing_floor:
        for pos in list(account.get("open_positions", [])):
            account["trade_log"].append({
                "type": "exit", "side": "liquidation", "symbol": pos.get("symbol", ""),
                "filled_price": pos.get("entry_price", 0), "qty": pos.get("qty", 1),
                "reason": "trailing_drawdown_breach", "pnl": 0,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        account["open_positions"] = []
        account["trading_halted_reason"] = "trailing_drawdown_breach"
        save_virtual_account(account)
        log.critical("TRAILING DRAWDOWN BREACH: $%.2f <= floor $%.2f (peak $%.2f)", current_balance, trailing_floor, peak_balance)
        return {
            "status": "halted", "trades_made": 0, "open_positions": 0,
            "account_balance": current_balance, "peak_balance": peak_balance,
            "trailing_dd_current": trailing_dd_current, "trailing_dd_limit": TRAILING_DD_PCT,
            "summary": f"TRAILING DRAWDOWN BREACH — balance ${current_balance:.2f} hit floor ${trailing_floor:.2f}",
            "escalate": True, "escalation_reason": "trailing_drawdown_breach", "errors": [],
        }

    if cycle_state is None:
        state_file = AGENT_NETWORK_STATE / "spx500_cycle_state.json"
        cycle_state = _load_file(state_file)

    strategy_id     = "Bollinger Lower Band Touch with ATR Expansion Confirmation"
    timeframe       = "1d"   # fallback — daily candles
    strategy_schema = None
    if cycle_state:
        strat       = cycle_state.get("active_strategy", {})
        strategy_id = (
            strat.get("strategy_name")
            or strat.get("strategy_id")
            or strategy_id
        )
        strategy_schema = strat.get("strategy_schema")
        # Read timeframe from the strategy schema (15m, 1h, 4h, 1d)
        schema_tf = (
            (strategy_schema or {})
                 .get("metadata", {})
                 .get("timeframe", "1d")
        )
        if schema_tf in _TF_MAP:
            timeframe = schema_tf

    # Multi-timeframe: check for a separate entry timeframe
    entry_timeframe = None
    if cycle_state:
        entry_timeframe = (
            (strategy_schema or {})
                 .get("metadata", {})
                 .get("entry_timeframe")
        )
    # Only keep entry_timeframe if it's valid and different from primary
    if entry_timeframe and (entry_timeframe not in _TF_MAP or entry_timeframe == timeframe):
        entry_timeframe = None

    # Read session preference from strategy schema (rth, extended, london, or all)
    session = "all"  # default
    if strategy_schema:
        session = (strategy_schema or {}).get("metadata", {}).get("session", "all")

    if entry_timeframe:
        log.info(
            "Virtual trader running MTF: primary=%s entry=%s session=%s (strategy: %s)",
            timeframe, entry_timeframe, session, strategy_id,
        )
    else:
        log.info("Virtual trader running on %s timeframe, session=%s (strategy: %s)", timeframe, session, strategy_id)

    # ── VIX-based sizing ──────────────────────────────────────────────────────
    vix = _fetch_vix()
    if vix > 35:
        log.warning("VIX=%.1f > 35: halting new entries (extreme volatility)", vix)
        vix_halt = True
        vix_size_factor = 0.0
    elif vix > 25:
        log.warning("VIX=%.1f > 25: reducing position size 50%%", vix)
        vix_halt = False
        vix_size_factor = 0.5
    else:
        vix_halt = False
        vix_size_factor = 1.0

    risk_multiplier = 1.0
    if cycle_state:
        raw_rm = cycle_state.get("risk_multiplier", 1.0)
        risk_multiplier = max(0.25, min(float(raw_rm), 1.0))

    now         = datetime.now(timezone.utc)
    balance     = account.get("account_balance", DEFAULT_BALANCE)

    today_str = datetime.now(timezone.utc).date().isoformat()
    # Reset daily tracking if it's a new day
    if account.get("trading_date") != today_str:
        account["trading_date"]    = today_str
        account["daily_pnl"]       = 0.0
        account["loss_streak"]     = account.get("loss_streak", 0)  # keep streak across days
        account["trading_halted"]  = False  # reset halt at start of new day

    daily_pnl      = account.get("daily_pnl", 0.0)
    loss_streak    = account.get("loss_streak", 0)
    trading_halted = account.get("trading_halted", False)

    # Check circuit breakers — including cooldown expiry for streak halt
    post_cooldown_half = False
    if trading_halted and account.get("halt_resume_time"):
        if datetime.now(timezone.utc).timestamp() > account["halt_resume_time"]:
            account["trading_halted"] = False
            account["loss_streak"] = 0
            trading_halted = False
            loss_streak = 0
            post_cooldown_half = True
            log.info("Circuit breaker cooldown expired — resuming at half size")

    if not trading_halted:
        if daily_pnl < -(balance * DAILY_LOSS_HALT_PCT / 100):
            trading_halted = True
            account["trading_halted"] = True
            # Force-close ALL open positions on daily halt
            for pos in list(account.get("open_positions", [])):
                account["trade_log"].append({
                    "type": "exit", "side": "liquidation", "symbol": pos.get("symbol", ""),
                    "filled_price": pos.get("entry_price", 0), "qty": pos.get("qty", 1),
                    "reason": "daily_loss_halt_liquidation", "pnl": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            account["open_positions"] = []
            log.warning("DAILY LOSS HALT: all positions force-closed (daily PnL: %.2f)", daily_pnl)
        elif loss_streak >= STREAK_HALT_COUNT:
            trading_halted = True
            account["trading_halted"] = True
            account["halt_resume_time"] = (datetime.now(timezone.utc).timestamp() + 1800)  # 30 min cooldown
            log.warning("CIRCUIT BREAKER: %d consecutive losses", loss_streak)

    risk_usd    = balance * (account.get("risk_per_trade_pct", RISK_PER_TRADE_PCT) / 100) * risk_multiplier
    if post_cooldown_half:
        risk_usd *= 0.5
    trades_made = []
    errors      = []

    # ── Pre-fetch all instruments for intermarket signal access ──────────
    _all_ohlcv = {}
    _all_ohlcv_entry = {}
    for _im_name, _im_ticker in INSTRUMENTS.items():
        _all_ohlcv[_im_name] = fetch_ohlcv(_im_ticker, timeframe, session=session)
        if entry_timeframe:
            _all_ohlcv_entry[_im_name] = fetch_ohlcv(_im_ticker, entry_timeframe, session=session)

    for display_name, ticker in INSTRUMENTS.items():
        try:
            if trading_halted:
                log.warning("Trading halted (circuit breaker) — skipping %s", display_name)
                continue

            ohlcv = _all_ohlcv.get(display_name)
            if not ohlcv:
                errors.append(f"No OHLCV data for {display_name} ({ticker})")
                continue

            # Time-of-day filter — if schema restricts trading hours, check now
            schema_metadata = (strategy_schema or {}).get("metadata", {})
            if not _check_time_filter(schema_metadata):
                et_hour = (datetime.now(timezone.utc).hour - 4) % 24
                log.info("Skipping %s: outside allowed trading hours (ET hour: %d)", display_name, et_hour)
                continue

            # Entry-timeframe OHLCV (None when single-timeframe — backward compat)
            ohlcv_entry = _all_ohlcv_entry.get(display_name)

            signals       = get_strategy_signals(strategy_id, ohlcv,
                                                 strategy_schema=strategy_schema,
                                                 intermarket_data=_all_ohlcv,
                                                 entry_ohlcv=ohlcv_entry)
            current_price = signals["current_price"]

            open_pos = next(
                (p for p in account["open_positions"] if p["symbol"] == display_name),
                None,
            )

            # ── Close open position ──────────────────────────────────────────
            if open_pos and (signals["exit"] or _stop_hit(open_pos, current_price)):
                entry_price = open_pos["entry_price"]
                side        = open_pos["side"]
                qty         = open_pos["qty"]
                entry_risk  = open_pos["risk_usd"]

                # Apply slippage on exit (opposite direction to entry)
                if side == "buy":
                    fill_price = round(current_price * (1 - _slippage_bps(display_name) / 10000), 4)
                else:
                    fill_price = round(current_price * (1 + _slippage_bps(display_name) / 10000), 4)

                pnl = (
                    (fill_price - entry_price) * qty
                    if side == "buy"
                    else (entry_price - fill_price) * qty
                )
                r_multiple = round(pnl / entry_risk, 2) if entry_risk else 0.0

                stop_reason = "Stop loss triggered" if _stop_hit(open_pos, current_price) else ""
                # Item 35 — fill tracking: expected price is the last bar's close
                # (current_price at signal evaluation); actual fill is after slippage.
                exit_slippage_bps = round(
                    abs(fill_price - current_price) / current_price * 10000, 2
                ) if current_price else 0.0
                exit_record = {
                    "order_id":    open_pos["order_id"],
                    "type":        "exit",
                    "strategy_id": strategy_id,
                    "side":        "sell" if side == "buy" else "buy",
                    "symbol":      display_name,
                    "filled_price": fill_price,
                    "qty":         qty,
                    "risk_usd":    entry_risk,
                    "reason":      stop_reason or signals["reason"] or "Exit signal",
                    "pnl":         round(pnl, 2),
                    "r_multiple":  r_multiple,
                    "timestamp":   now.isoformat(),
                    # Fill-tracking fields for empirical slippage model
                    "expected_price": current_price,
                    "actual_fill":    fill_price,
                    "slippage_bps":   exit_slippage_bps,
                }
                account["trade_log"].append(exit_record)
                account["open_positions"] = [
                    p for p in account["open_positions"]
                    if p["symbol"] != display_name
                ]
                balance = round(balance + pnl, 2)
                account["account_balance"] = balance
                risk_usd = balance * (account.get("risk_per_trade_pct", RISK_PER_TRADE_PCT) / 100) * risk_multiplier
                account["daily_pnl"] = round(account.get("daily_pnl", 0.0) + pnl, 2)
                if pnl > 0:
                    account["loss_streak"] = 0
                else:
                    account["loss_streak"] = account.get("loss_streak", 0) + 1
                    # Re-check streak circuit breaker
                    if account["loss_streak"] >= STREAK_HALT_COUNT:
                        account["trading_halted"] = True
                        account["halt_resume_time"] = (datetime.now(timezone.utc).timestamp() + 1800)  # 30 min cooldown
                        trading_halted = True
                        log.warning("CIRCUIT BREAKER triggered: %d consecutive losses", account["loss_streak"])
                trades_made.append(exit_record)
                # Item 35 — append to empirical fill-tracking log
                if "fill_tracking" not in account:
                    account["fill_tracking"] = []
                account["fill_tracking"].append({
                    "type":           "exit",
                    "symbol":         display_name,
                    "strategy_id":    strategy_id,
                    "expected_price": current_price,
                    "actual_fill":    fill_price,
                    "slippage_bps":   exit_slippage_bps,
                    "timestamp":      now.isoformat(),
                })
                log.info("CLOSE %s @ %.2f | PnL=%.2f R=%.2f",
                         display_name, fill_price, pnl, r_multiple)

            # ── Open new position ────────────────────────────────────────────
            elif not open_pos and signals["entry"] and current_price > 0:
                # VIX halt — skip new entries in extreme volatility
                if vix_halt:
                    log.warning("Skipping %s entry: VIX=%.1f > 35 (extreme volatility halt)",
                                display_name, vix)
                    continue

                # Check correlation with existing open positions
                max_corr = max(
                    (_correlation(display_name, p["symbol"]) for p in account["open_positions"]),
                    default=0.0
                )
                if max_corr > MAX_PORTFOLIO_CORRELATION:
                    log.warning("Skipping %s entry: correlation %.2f with open position exceeds %.2f",
                                display_name, max_corr, MAX_PORTFOLIO_CORRELATION)
                    continue

                # Contract limit enforcement
                current_contracts = sum(p.get("qty", 1) for p in account["open_positions"])
                if current_contracts >= MAX_TOTAL_CONTRACTS:
                    log.info("CONTRACT LIMIT: total %d >= max %d — skipping %s entry",
                             current_contracts, MAX_TOTAL_CONTRACTS, display_name)
                    continue

                # Volatility-normalize: use ATR relative to SPX baseline for sizing
                atr_values = _calc_atr(ohlcv["high"], ohlcv["low"], ohlcv["close"])
                current_atr = next((v for v in reversed(atr_values) if v is not None), None)
                current_price_for_atr = current_price

                # SPX500 baseline ATR% (approximate: ~0.8% daily ATR)
                BASELINE_ATR_PCT = 0.008
                instrument_atr_pct = (current_atr / current_price_for_atr) if (current_atr and current_price_for_atr > 0) else BASELINE_ATR_PCT
                vol_adjustment = BASELINE_ATR_PCT / max(instrument_atr_pct, 0.001)
                vol_adjustment = max(0.5, min(2.0, vol_adjustment))  # clamp to 50%-200%
                adjusted_risk_usd = risk_usd * vix_size_factor * vol_adjustment

                # Apply slippage on entry based on side
                if signals["side"] == "buy":
                    fill_price = round(current_price * (1 + _slippage_bps(display_name) / 10000), 4)
                else:
                    fill_price = round(current_price * (1 - _slippage_bps(display_name) / 10000), 4)

                qty      = max(1, int(adjusted_risk_usd / (fill_price * STOP_PCT)))
                qty      = min(qty, MAX_CONTRACTS_PER_INSTRUMENT)
                qty      = min(qty, MAX_TOTAL_CONTRACTS - current_contracts)
                if qty <= 0:
                    continue
                order_id = f"VA-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                stop     = (
                    round(fill_price * (1 - STOP_PCT), 2)
                    if signals["side"] == "buy"
                    else round(fill_price * (1 + STOP_PCT), 2)
                )

                # Item 35 — fill tracking for entry: expected = signal bar close,
                # actual = fill price after slippage model.
                entry_slippage_bps = round(
                    abs(fill_price - current_price) / current_price * 10000, 2
                ) if current_price else 0.0
                entry_record = {
                    "order_id":    order_id,
                    "type":        "entry",
                    "strategy_id": strategy_id,
                    "side":        signals["side"],
                    "symbol":      display_name,
                    "filled_price": fill_price,
                    "qty":         qty,
                    "risk_usd":    round(adjusted_risk_usd, 2),
                    "reason":      signals["reason"],
                    "pnl":         None,
                    "r_multiple":  None,
                    "timestamp":   now.isoformat(),
                    # Fill-tracking fields for empirical slippage model
                    "expected_price": current_price,
                    "actual_fill":    fill_price,
                    "slippage_bps":   entry_slippage_bps,
                }
                account["trade_log"].append(entry_record)
                account["open_positions"].append({
                    "order_id":   order_id,
                    "symbol":     display_name,
                    "side":       signals["side"],
                    "entry_price": fill_price,
                    "qty":        qty,
                    "risk_usd":   round(adjusted_risk_usd, 2),
                    "stop_loss":  stop,
                    "opened_at":  now.isoformat(),
                })
                trades_made.append(entry_record)
                # Item 35 — append to empirical fill-tracking log
                if "fill_tracking" not in account:
                    account["fill_tracking"] = []
                account["fill_tracking"].append({
                    "type":           "entry",
                    "symbol":         display_name,
                    "strategy_id":    strategy_id,
                    "expected_price": current_price,
                    "actual_fill":    fill_price,
                    "slippage_bps":   entry_slippage_bps,
                    "timestamp":      now.isoformat(),
                })
                log.info("OPEN %s %s @ %.2f | qty=%d risk=%.2f (vol_adj=%.2f vix_sf=%.2f)",
                         signals["side"].upper(), display_name, fill_price, qty,
                         adjusted_risk_usd, vol_adjustment, vix_size_factor)

        except Exception as e:
            errors.append(f"{display_name}: {e}")
            log.error("Virtual account error for %s: %s", display_name, e)

    save_virtual_account(account)

    status = "success" if not errors else ("partial" if trades_made or not errors else "failed")
    if errors and not trades_made:
        status = "failed"

    return {
        "status":           status,
        "trades_made":      len(trades_made),
        "open_positions":   len(account["open_positions"]),
        "account_balance":  account["account_balance"],
        "strategy_id":      strategy_id,
        "errors":           errors,
        "summary":          _build_summary(trades_made, account, strategy_id),
        "trading_halted":   account.get("trading_halted", False),
        "daily_pnl":        account.get("daily_pnl", 0.0),
        "loss_streak":      account.get("loss_streak", 0),
        "risk_multiplier":  risk_multiplier,
        "vix":              vix,
        "vix_size_factor":  vix_size_factor,
    }


def _build_summary(trades: list, account: dict, strategy_id: str) -> str:
    balance    = account.get("account_balance", 0)
    initial    = account.get("initial_balance", balance)
    pnl_total  = round(balance - initial, 2)
    open_count = len(account.get("open_positions", []))

    if not trades:
        return (
            f"No new trades. Strategy: {strategy_id} | "
            f"Open: {open_count} | Balance: ${balance:,.2f} (Total PnL: ${pnl_total:+.2f})"
        )

    entries    = [t for t in trades if t["type"] == "entry"]
    exits      = [t for t in trades if t["type"] == "exit"]
    pnl_today  = sum(t.get("pnl") or 0 for t in exits)

    parts = []
    if entries:
        parts.append(f"Opened: {', '.join(t['symbol'] for t in entries)}")
    if exits:
        parts.append(f"Closed: {', '.join(t['symbol'] for t in exits)} (PnL: ${pnl_today:+.2f})")
    parts.append(f"Balance: ${balance:,.2f} (Total: ${pnl_total:+.2f})")
    return " | ".join(parts)
