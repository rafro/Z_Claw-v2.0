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
        return {"SPX500": "^GSPC", "XAUUSD": "GC=F"}


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
SLIPPAGE_BPS       = 5     # 5 basis points per fill (0.05%)
DAILY_LOSS_HALT_PCT = 3.0  # halt if daily PnL < -3% of account
STREAK_HALT_COUNT   = 5    # halt after N consecutive losses
TRAILING_DD_PCT     = 6.0  # trailing drawdown limit (% from peak)
MAX_TOTAL_CONTRACTS      = 10   # max total contracts across all instruments
MAX_CONTRACTS_PER_INSTRUMENT = 5  # max contracts per single instrument

# Known pairwise correlations (approximate, based on historical data)
# High correlation = potential double-exposure risk
INSTRUMENT_CORRELATIONS = {
    ("SPX500", "XAUUSD"): -0.15,
    ("SPX500", "CRUDE"):   0.40,
    ("SPX500", "BONDS"):  -0.30,
    ("XAUUSD", "CRUDE"):   0.25,
    ("XAUUSD", "BONDS"):   0.20,
    ("CRUDE",  "BONDS"):  -0.15,
}
MAX_PORTFOLIO_CORRELATION = 0.80


def _correlation(inst_a: str, inst_b: str) -> float:
    key = (inst_a, inst_b) if (inst_a, inst_b) in INSTRUMENT_CORRELATIONS else (inst_b, inst_a)
    return INSTRUMENT_CORRELATIONS.get(key, 0.0)


# Per-instrument slippage overrides (basis points)
_INSTRUMENT_SLIPPAGE = {
    "SPX500": 3,   # most liquid
    "XAUUSD": 8,   # gold spreads wider
    "CRUDE":  5,   # oil moderate
    "BONDS":  3,   # treasuries liquid
}


def _slippage_bps(instrument: str) -> float:
    """Return per-instrument slippage in basis points."""
    return _INSTRUMENT_SLIPPAGE.get(instrument, SLIPPAGE_BPS)


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


def fetch_ohlcv(ticker: str, timeframe: str = "1d") -> Optional[dict]:
    """
    Fetch OHLCV via yfinance for the given timeframe (15m, 1h, 4h, 1d).
    4h is fetched as 1h then resampled. Returns dict with lists:
    date, open, high, low, close, volume.
    """
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
        log.debug("Fetched %d %s bars for %s", len(ohlcv["close"]), timeframe, ticker)
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


def _calc_rsi(prices: list, period: int = 14) -> list:
    """Wilder RSI."""
    if len(prices) < period + 1:
        return [None] * len(prices)
    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result: list = [None] * period
    rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
    result.append(100 - 100 / (1 + rs))
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else 100.0
        result.append(100 - 100 / (1 + rs))
    return result


def _calc_macd(prices: list, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list, list, list]:
    """MACD line, signal line, histogram."""
    ema_fast = _calc_ema(prices, fast)
    ema_slow = _calc_ema(prices, slow)
    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    valid_macd = [v for v in macd_line if v is not None]
    signal_line_raw = _calc_ema(valid_macd, signal) if len(valid_macd) >= signal else [None] * len(valid_macd)
    # Align signal_line back to full length
    signal_line: list = [None] * (len(macd_line) - len(signal_line_raw)) + signal_line_raw
    histogram = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line)
    ]
    return macd_line, signal_line, histogram


def _calc_stochastic(high: list, low: list, close: list,
                     k_period: int = 14, d_period: int = 3) -> tuple[list, list]:
    """Stochastic %K and %D."""
    k_vals: list = []
    for i in range(len(close)):
        if i < k_period - 1:
            k_vals.append(None)
        else:
            h = max(high[i - k_period + 1:i + 1])
            l = min(low[i - k_period + 1:i + 1])
            if h == l:
                k_vals.append(50.0)
            else:
                k_vals.append((close[i] - l) / (h - l) * 100)
    # %D = SMA of %K
    d_vals: list = []
    valid_k = [v for v in k_vals if v is not None]
    for i in range(len(k_vals)):
        if k_vals[i] is None:
            d_vals.append(None)
        else:
            idx_in_valid = sum(1 for v in k_vals[:i + 1] if v is not None) - 1
            if idx_in_valid < d_period - 1:
                d_vals.append(None)
            else:
                window = valid_k[idx_in_valid - d_period + 1:idx_in_valid + 1]
                d_vals.append(sum(window) / d_period)
    return k_vals, d_vals


# ── Multi-factor scoring ─────────────────────────────────────────────────────

SIGNAL_WEIGHTS = {
    "trend":      0.35,
    "momentum":   0.30,
    "volatility": 0.20,
    "volume":     0.00,
    "structure":  0.15,
}

ENTRY_THRESHOLD = 0.60
EXIT_THRESHOLD  = 0.35


def _score_trend(close: list) -> float:
    """EMA alignment score: 0=bearish, 0.5=neutral, 1=bullish."""
    ema20 = _last(_calc_ema(close, 20))
    ema50 = _last(_calc_ema(close, 50))
    if ema20 is None or ema50 is None:
        return 0.5
    price = close[-1]
    score = 0.5
    if price > ema20 > ema50:
        score = 1.0
    elif price > ema20:
        score = 0.75
    elif price < ema20 < ema50:
        score = 0.0
    elif price < ema20:
        score = 0.25
    return score


def _score_momentum(close: list, high: list, low: list) -> float:
    """RSI + MACD histogram + Stochastic — SYMMETRIC scoring."""
    rsi_val = _last(_calc_rsi(close, 14))
    _, _, hist = _calc_macd(close)
    hist_val = _last(hist)
    k_vals, d_vals = _calc_stochastic(high, low, close)
    k_val = _last(k_vals)
    d_val = _last(d_vals)

    scores = []

    # RSI — symmetric
    if rsi_val is not None:
        if 55 <= rsi_val <= 70:
            scores.append(0.75)
        elif 30 <= rsi_val <= 45:
            scores.append(0.25)
        elif 45 < rsi_val < 55:
            scores.append(0.5)
        elif rsi_val > 70:
            scores.append(0.6)   # overbought — slightly bullish but caution
        else:  # < 30
            scores.append(0.4)   # oversold — slightly bearish but caution
    else:
        scores.append(0.5)

    # MACD histogram — symmetric
    if hist_val is not None:
        if hist_val > 0:
            scores.append(0.75)
        elif hist_val < 0:
            scores.append(0.25)
        else:
            scores.append(0.5)
    else:
        scores.append(0.5)

    # Stochastic — symmetric
    if k_val is not None and d_val is not None:
        if k_val > d_val and k_val < 80:
            scores.append(0.75)
        elif k_val < d_val and k_val > 20:
            scores.append(0.25)
        else:
            scores.append(0.5)
    else:
        scores.append(0.5)

    return sum(scores) / len(scores)


def _score_volatility(high: list, low: list, close: list) -> float:
    """ATR expansion = opportunity (higher score). Contraction = caution."""
    atr = _calc_atr(high, low, close)
    expanding = _atr_expanding(atr)
    if expanding is None:
        return 0.5
    return 0.75 if expanding else 0.35


def _score_volume() -> float:
    """Volume scoring — returns 0.5 neutral for daily bars (no intraday volume profile)."""
    return 0.5


def _score_structure(close: list) -> float:
    """Price relative to Bollinger Bands — mean-reversion / breakout signal."""
    bb_upper, bb_mid, bb_lower = _calc_bollinger(close)
    upper = _last(bb_upper)
    lower = _last(bb_lower)
    mid = _last(bb_mid)
    if upper is None or lower is None or mid is None:
        return 0.5
    price = close[-1]
    band_width = upper - lower
    if band_width <= 0:
        return 0.5
    position = (price - lower) / band_width  # 0 = at lower, 1 = at upper
    # Near lower band = potential long (higher score), near upper = potential short (lower score)
    # Middle = neutral
    if position < 0.2:
        return 0.75   # near lower band — bullish reversal zone
    elif position > 0.8:
        return 0.25   # near upper band — bearish reversal zone
    else:
        return 0.5


def _composite_score(close: list, high: list, low: list) -> float:
    """Weighted composite of all factor scores."""
    scores = {
        "trend":      _score_trend(close),
        "momentum":   _score_momentum(close, high, low),
        "volatility": _score_volatility(high, low, close),
        "volume":     _score_volume(),
        "structure":  _score_structure(close),
    }
    total = sum(scores[k] * SIGNAL_WEIGHTS[k] for k in SIGNAL_WEIGHTS)
    return round(total, 4)


# ── Signal engine ──────────────────────────────────────────────────────────────


def _resolve_from_schema(strategy_schema: dict, ohlcv: dict) -> Optional[dict]:
    """
    PRIMARY signal path: read the strategy schema and use its exact parameters.

    This ensures the live signals match what was backtested. The schema contains
    the indicator type, its parameters, entry/exit triggers, confirmation logic,
    stop loss configuration, and directional bias.

    Returns the standard signal dict, or None if resolution fails (so the caller
    can fall back to the name-parsing logic).
    """
    try:
        close  = ohlcv["close"]
        high   = ohlcv["high"]
        low    = ohlcv["low"]
        volume = ohlcv.get("volume", [0.0] * len(close))

        current_price = close[-1] if close else 0.0
        result = {
            "entry": False, "exit": False,
            "side": "buy", "reason": "",
            "current_price": current_price,
        }

        if not close or len(close) < 40:
            result["reason"] = "Insufficient price history (<40 bars)"
            return result

        # ── Read schema sections (with safe defaults) ─────────────────────────
        primary    = strategy_schema.get("primary_indicator", {})
        confirm    = strategy_schema.get("confirmation", {})
        entry_cfg  = strategy_schema.get("entry", {})
        exit_cfg   = strategy_schema.get("exit", {})
        stop_cfg   = strategy_schema.get("stop_loss", {})
        metadata   = strategy_schema.get("metadata", {})

        indicator_type = primary.get("type", "").lower()
        if not indicator_type:
            return None  # No indicator type — cannot resolve, fall back

        direction = metadata.get("direction", "long").lower()
        result["side"] = "buy" if direction == "long" else "sell"

        entry_trigger = entry_cfg.get("trigger", "").lower()
        exit_trigger  = exit_cfg.get("trigger", "").lower()

        # ── Compute confirmation signal ───────────────────────────────────────
        confirm_type = confirm.get("type", "").lower()
        confirmation_met = True  # default: no confirmation required
        confirmation_desc = ""

        if confirm_type == "atr_expansion":
            atr_period = confirm.get("period", 14)
            threshold  = confirm.get("threshold", 1.0)
            atr = _calc_atr(high, low, close, period=atr_period)
            valid_atr = [v for v in atr if v is not None]
            if len(valid_atr) >= 6:
                current_atr = valid_atr[-1]
                recent_avg  = sum(valid_atr[-6:-1]) / 5
                confirmation_met = current_atr > (recent_avg * threshold)
                confirmation_desc = (
                    f"ATR {'expanding' if confirmation_met else 'contracting'} "
                    f"(current={current_atr:.2f}, avg={recent_avg:.2f}, thresh={threshold}x)"
                )
            else:
                confirmation_met = True  # not enough data, don't block
                confirmation_desc = "ATR confirmation skipped (insufficient data)"

        elif confirm_type == "rsi":
            rsi_period = confirm.get("period", 14)
            rsi_vals = _calc_rsi(close, period=rsi_period)
            rsi_val = _last(rsi_vals)
            rsi_threshold = confirm.get("threshold", 50)
            if rsi_val is not None:
                if direction == "long":
                    confirmation_met = rsi_val < rsi_threshold
                else:
                    confirmation_met = rsi_val > rsi_threshold
                confirmation_desc = f"RSI={rsi_val:.1f} (threshold={rsi_threshold})"
            else:
                confirmation_desc = "RSI confirmation skipped (insufficient data)"

        elif confirm_type == "macd":
            fast = confirm.get("fast", 12)
            slow = confirm.get("slow", 26)
            sig  = confirm.get("signal_period", 9)
            macd_line, sig_line, hist = _calc_macd(close, fast, slow, sig)
            h = _last(hist)
            if h is not None:
                confirmation_met = h > 0 if direction == "long" else h < 0
                confirmation_desc = f"MACD histogram={'positive' if h > 0 else 'negative'}"
            else:
                confirmation_desc = "MACD confirmation skipped (insufficient data)"

        elif confirm_type == "volume_above_avg":
            lookback = confirm.get("period", 20)
            if len(volume) >= lookback:
                avg_vol = sum(volume[-lookback:]) / lookback
                confirmation_met = volume[-1] > avg_vol * confirm.get("threshold", 1.0)
                confirmation_desc = f"Volume {'above' if confirmation_met else 'below'} avg"
            else:
                confirmation_desc = "Volume confirmation skipped (insufficient data)"

        elif confirm_type == "ema_crossover":
            fast_p = confirm.get("fast_period", 12)
            slow_p = confirm.get("slow_period", 26)
            ema_f = _calc_ema(close, fast_p)
            ema_s = _calc_ema(close, slow_p)
            ef, es = _last(ema_f), _last(ema_s)
            if ef is not None and es is not None:
                confirmation_met = ef > es if direction == "long" else ef < es
                confirmation_desc = f"EMA{fast_p}={'above' if ef > es else 'below'} EMA{slow_p}"
            else:
                confirmation_desc = "EMA crossover confirmation skipped (insufficient data)"

        elif confirm_type == "stochastic":
            k_p = confirm.get("k_period", 14)
            d_p = confirm.get("d_period", 3)
            k_vals, d_vals = _calc_stochastic(high, low, close, k_p, d_p)
            k_val = _last(k_vals)
            threshold = confirm.get("threshold", 20)
            if k_val is not None:
                if direction == "long":
                    confirmation_met = k_val < threshold  # oversold
                else:
                    confirmation_met = k_val > (100 - threshold)  # overbought
                confirmation_desc = f"Stochastic %K={k_val:.1f} (threshold={threshold})"
            else:
                confirmation_desc = "Stochastic confirmation skipped (insufficient data)"

        elif confirm_type:
            # Unknown confirmation type — don't block the trade
            confirmation_desc = f"Unknown confirmation type '{confirm_type}' — skipped"

        # ── Compute primary indicator values and entry/exit signals ────────────
        entry_signal = False
        exit_signal  = False
        reason_parts = []

        if indicator_type == "bollinger_bands":
            period   = primary.get("period", 20)
            std_dev  = primary.get("std_dev", 2.0)
            bb_upper, bb_mid, bb_lower = _calc_bollinger(close, period, std_dev)
            prev_close = close[-2] if len(close) >= 2 else current_price
            curr_lower = _last(bb_lower)
            prev_lower = _last(bb_lower[:-1]) if len(bb_lower) >= 2 else None
            curr_upper = _last(bb_upper)
            curr_mid   = _last(bb_mid)

            if curr_lower is None:
                result["reason"] = f"Bollinger({period},{std_dev}) insufficient data"
                return result

            # Entry triggers
            if entry_trigger == "price_touches_lower_band":
                bb_touch = (prev_lower is not None) and (prev_close <= prev_lower)
                if bb_touch and current_price > curr_lower:
                    entry_signal = True
                    reason_parts.append(
                        f"Bollinger({period},{std_dev}) lower touch ${curr_lower:.2f} "
                        f"→ rebounding ${current_price:.2f}"
                    )
            elif entry_trigger == "price_touches_upper_band":
                bb_touch = (curr_upper is not None) and (prev_close >= curr_upper if len(close) >= 2 else False)
                if bb_touch and current_price < curr_upper:
                    entry_signal = True
                    reason_parts.append(
                        f"Bollinger({period},{std_dev}) upper touch ${curr_upper:.2f} "
                        f"→ reversing ${current_price:.2f}"
                    )
            elif entry_trigger == "price_below_lower_band":
                if current_price < curr_lower:
                    entry_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} below Bollinger lower ${curr_lower:.2f}"
                    )
            else:
                # Default Bollinger entry: lower band touch
                bb_touch = (prev_lower is not None) and (prev_close <= prev_lower)
                if bb_touch and current_price > curr_lower:
                    entry_signal = True
                    reason_parts.append(
                        f"Bollinger({period},{std_dev}) lower touch → rebounding"
                    )

            # Exit triggers
            if exit_trigger == "price_crosses_middle_band":
                if curr_mid and current_price >= curr_mid:
                    exit_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} crossed Bollinger mid ${curr_mid:.2f}"
                    )
            elif exit_trigger == "price_crosses_upper_band":
                if curr_upper and current_price >= curr_upper:
                    exit_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} crossed Bollinger upper ${curr_upper:.2f}"
                    )
            else:
                # Default exit: middle band
                if curr_mid and current_price >= curr_mid:
                    exit_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} at Bollinger mid ${curr_mid:.2f}"
                    )

        elif indicator_type == "ema_crossover":
            fast_period = primary.get("fast_period", 20)
            slow_period = primary.get("slow_period", 50)
            ema_fast = _calc_ema(close, fast_period)
            ema_slow = _calc_ema(close, slow_period)
            ef = _last(ema_fast)
            es = _last(ema_slow)

            if ef is None or es is None:
                result["reason"] = f"EMA({fast_period}/{slow_period}) insufficient data"
                return result

            # Check for crossover (current bar vs previous)
            ef_prev = _last(ema_fast[:-1]) if len(ema_fast) >= 2 else None
            es_prev = _last(ema_slow[:-1]) if len(ema_slow) >= 2 else None

            if entry_trigger == "fast_crosses_above_slow" or not entry_trigger:
                if ef > es:
                    crossed = (ef_prev is not None and es_prev is not None and ef_prev <= es_prev)
                    entry_signal = crossed or (ef > es)  # signal if above, strong if just crossed
                    reason_parts.append(
                        f"EMA{fast_period} ${ef:.2f} above EMA{slow_period} ${es:.2f}"
                    )
            elif entry_trigger == "fast_crosses_below_slow":
                if ef < es:
                    entry_signal = True
                    reason_parts.append(
                        f"EMA{fast_period} ${ef:.2f} below EMA{slow_period} ${es:.2f}"
                    )

            if exit_trigger == "fast_crosses_below_slow" or not exit_trigger:
                if ef < es:
                    exit_signal = True
                    reason_parts.append(
                        f"EMA{fast_period} ${ef:.2f} crossed below EMA{slow_period} ${es:.2f} — exit"
                    )
            elif exit_trigger == "fast_crosses_above_slow":
                if ef > es:
                    exit_signal = True
                    reason_parts.append(f"EMA crossover exit triggered")

        elif indicator_type == "ema_above_price":
            period = primary.get("period", 38)
            ema = _calc_ema(close, period)
            ema_val = _last(ema)

            if ema_val is None:
                result["reason"] = f"EMA({period}) insufficient data"
                return result

            if entry_trigger == "price_above_ema" or not entry_trigger:
                if current_price > ema_val:
                    entry_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} above EMA{period} ${ema_val:.2f}"
                    )
            elif entry_trigger == "price_below_ema":
                if current_price < ema_val:
                    entry_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} below EMA{period} ${ema_val:.2f}"
                    )

            if exit_trigger == "price_below_ema" or not exit_trigger:
                if current_price < ema_val:
                    exit_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} below EMA{period} ${ema_val:.2f} — exit"
                    )
            elif exit_trigger == "price_above_ema":
                if current_price > ema_val:
                    exit_signal = True
                    reason_parts.append(f"Price above EMA — exit")

        elif indicator_type == "rsi":
            period     = primary.get("period", 14)
            overbought = primary.get("overbought", 70)
            oversold   = primary.get("oversold", 30)
            rsi_vals   = _calc_rsi(close, period=period)
            rsi_val    = _last(rsi_vals)

            if rsi_val is None:
                result["reason"] = f"RSI({period}) insufficient data"
                return result

            if entry_trigger == "rsi_oversold" or (not entry_trigger and direction == "long"):
                if rsi_val < oversold:
                    entry_signal = True
                    reason_parts.append(f"RSI({period})={rsi_val:.1f} < {oversold} (oversold)")
            elif entry_trigger == "rsi_overbought" or (not entry_trigger and direction == "short"):
                if rsi_val > overbought:
                    entry_signal = True
                    reason_parts.append(f"RSI({period})={rsi_val:.1f} > {overbought} (overbought)")

            if exit_trigger == "rsi_overbought" or (not exit_trigger and direction == "long"):
                if rsi_val > overbought:
                    exit_signal = True
                    reason_parts.append(f"RSI({period})={rsi_val:.1f} > {overbought} — exit")
            elif exit_trigger == "rsi_oversold" or (not exit_trigger and direction == "short"):
                if rsi_val < oversold:
                    exit_signal = True
                    reason_parts.append(f"RSI({period})={rsi_val:.1f} < {oversold} — exit")

        elif indicator_type == "rsi_divergence":
            period     = primary.get("period", 14)
            lookback   = primary.get("lookback", 10)
            rsi_vals   = _calc_rsi(close, period=period)
            valid_rsi  = [v for v in rsi_vals if v is not None]

            if len(valid_rsi) < lookback:
                result["reason"] = f"RSI divergence({period}) insufficient data"
                return result

            rsi_val = valid_rsi[-1]
            # Bullish divergence: price makes lower low but RSI makes higher low
            price_lower = close[-1] < min(close[-lookback:-1])
            rsi_higher  = valid_rsi[-1] > min(valid_rsi[-lookback:-1])
            # Bearish divergence: price makes higher high but RSI makes lower high
            price_higher = close[-1] > max(close[-lookback:-1])
            rsi_lower    = valid_rsi[-1] < max(valid_rsi[-lookback:-1])

            if direction == "long":
                if price_lower and rsi_higher:
                    entry_signal = True
                    reason_parts.append(f"Bullish RSI divergence (RSI={rsi_val:.1f})")
                if price_higher and rsi_lower:
                    exit_signal = True
                    reason_parts.append(f"Bearish RSI divergence — exit (RSI={rsi_val:.1f})")
            else:
                if price_higher and rsi_lower:
                    entry_signal = True
                    reason_parts.append(f"Bearish RSI divergence (RSI={rsi_val:.1f})")
                if price_lower and rsi_higher:
                    exit_signal = True
                    reason_parts.append(f"Bullish RSI divergence — exit (RSI={rsi_val:.1f})")

        elif indicator_type == "macd":
            fast   = primary.get("fast", 12)
            slow   = primary.get("slow", 26)
            signal = primary.get("signal_period", 9)
            macd_line, sig_line, hist = _calc_macd(close, fast, slow, signal)
            m = _last(macd_line)
            s = _last(sig_line)
            h = _last(hist)

            if m is None or s is None:
                result["reason"] = f"MACD({fast},{slow},{signal}) insufficient data"
                return result

            h_prev = _last(hist[:-1]) if len(hist) >= 2 else None

            if entry_trigger == "macd_crosses_signal" or not entry_trigger:
                if direction == "long":
                    if h is not None and h > 0 and (h_prev is not None and h_prev <= 0):
                        entry_signal = True
                        reason_parts.append(f"MACD crossed above signal (hist={h:.4f})")
                    elif h is not None and h > 0:
                        entry_signal = True
                        reason_parts.append(f"MACD above signal (hist={h:.4f})")
                else:
                    if h is not None and h < 0 and (h_prev is not None and h_prev >= 0):
                        entry_signal = True
                        reason_parts.append(f"MACD crossed below signal (hist={h:.4f})")
                    elif h is not None and h < 0:
                        entry_signal = True
                        reason_parts.append(f"MACD below signal (hist={h:.4f})")

            if exit_trigger == "macd_crosses_signal" or not exit_trigger:
                if direction == "long" and h is not None and h < 0:
                    exit_signal = True
                    reason_parts.append(f"MACD below signal — exit (hist={h:.4f})")
                elif direction == "short" and h is not None and h > 0:
                    exit_signal = True
                    reason_parts.append(f"MACD above signal — exit (hist={h:.4f})")

        elif indicator_type == "stochastic":
            k_period = primary.get("k_period", 14)
            d_period = primary.get("d_period", 3)
            overbought = primary.get("overbought", 80)
            oversold   = primary.get("oversold", 20)
            k_vals, d_vals = _calc_stochastic(high, low, close, k_period, d_period)
            k_val = _last(k_vals)
            d_val = _last(d_vals)

            if k_val is None:
                result["reason"] = f"Stochastic({k_period},{d_period}) insufficient data"
                return result

            if direction == "long":
                if k_val < oversold:
                    entry_signal = True
                    reason_parts.append(f"Stochastic %K={k_val:.1f} < {oversold} (oversold)")
                if k_val > overbought:
                    exit_signal = True
                    reason_parts.append(f"Stochastic %K={k_val:.1f} > {overbought} — exit")
            else:
                if k_val > overbought:
                    entry_signal = True
                    reason_parts.append(f"Stochastic %K={k_val:.1f} > {overbought} (overbought)")
                if k_val < oversold:
                    exit_signal = True
                    reason_parts.append(f"Stochastic %K={k_val:.1f} < {oversold} — exit")

        elif indicator_type == "atr_expansion":
            atr_period = primary.get("period", 14)
            threshold  = primary.get("threshold", 1.0)
            atr = _calc_atr(high, low, close, period=atr_period)
            valid_atr = [v for v in atr if v is not None]

            if len(valid_atr) < 6:
                result["reason"] = f"ATR({atr_period}) insufficient data"
                return result

            current_atr = valid_atr[-1]
            recent_avg  = sum(valid_atr[-6:-1]) / 5
            expanding   = current_atr > (recent_avg * threshold)

            if expanding:
                entry_signal = True
                reason_parts.append(
                    f"ATR expanding: {current_atr:.2f} > {recent_avg:.2f}*{threshold}"
                )
            else:
                exit_signal = True
                reason_parts.append(
                    f"ATR contracting: {current_atr:.2f} <= {recent_avg:.2f}*{threshold} — exit"
                )

        elif indicator_type == "vwap":
            vwap = _calc_vwap(high, low, close, volume)
            vwap_val = _last(vwap)

            if vwap_val is None:
                result["reason"] = "VWAP insufficient data"
                return result

            if entry_trigger == "price_above_vwap" or (not entry_trigger and direction == "long"):
                if current_price > vwap_val:
                    entry_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} above VWAP ${vwap_val:.2f}"
                    )
            elif entry_trigger == "price_below_vwap" or (not entry_trigger and direction == "short"):
                if current_price < vwap_val:
                    entry_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} below VWAP ${vwap_val:.2f}"
                    )

            if exit_trigger == "price_below_vwap" or (not exit_trigger and direction == "long"):
                if current_price < vwap_val:
                    exit_signal = True
                    reason_parts.append(
                        f"Price ${current_price:.2f} below VWAP ${vwap_val:.2f} — exit"
                    )
            elif exit_trigger == "price_above_vwap" or (not exit_trigger and direction == "short"):
                if current_price > vwap_val:
                    exit_signal = True
                    reason_parts.append(f"Price above VWAP — exit")

        else:
            # Unrecognized indicator type — cannot resolve from schema
            return None

        # ── Apply confirmation filter to entry signal ─────────────────────────
        if entry_signal and not confirmation_met:
            entry_signal = False
            reason_parts.append(f"Entry blocked: {confirmation_desc}")

        if entry_signal and confirmation_met and confirmation_desc:
            reason_parts.append(f"Confirmed: {confirmation_desc}")

        # ── Build result ──────────────────────────────────────────────────────
        result["entry"]  = entry_signal and not exit_signal
        result["exit"]   = exit_signal
        result["reason"] = " | ".join(reason_parts) if reason_parts else "No signal"

        # ── Annotate stop loss info from schema (consumed by position sizing) ─
        if stop_cfg:
            stop_type = stop_cfg.get("type", "")
            if stop_type == "atr_based":
                atr = _calc_atr(high, low, close)
                current_atr = _last(atr)
                if current_atr:
                    multiplier = stop_cfg.get("multiplier", 2.0)
                    result["stop_distance"] = current_atr * multiplier
            elif stop_type == "percent":
                pct = stop_cfg.get("percent", 1.0)
                result["stop_distance"] = current_price * (pct / 100)
            elif stop_type == "fixed":
                result["stop_distance"] = stop_cfg.get("distance", current_price * 0.01)

        log.info("Schema-resolved signal for %s: entry=%s exit=%s side=%s | %s",
                 indicator_type, result["entry"], result["exit"],
                 result["side"], result["reason"])
        return result

    except Exception as e:
        log.warning("Schema resolution failed (%s) — falling back to name-parsing", e)
        return None


def get_strategy_signals(strategy_id: str, ohlcv: dict, strategy_schema: dict = None) -> dict:
    """

    # Priority 1: Schema-driven resolution (uses exact backtested parameters)
    if strategy_schema:
        schema_result = _resolve_from_schema(strategy_schema, ohlcv)
        if schema_result is not None:
            return schema_result

    # Priority 2: Name-parsing (hardcoded defaults — legacy fallback)
    Generate entry/exit signals based on strategy_id and OHLCV data.
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

    atr          = _calc_atr(high, low, close)
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

    # ── Multi-factor composite fallback ───────────────────────────────────────
    else:
        score = _composite_score(close, high, low)
        if score >= ENTRY_THRESHOLD:
            result["entry"] = True
            result["side"]  = "buy"
            result["reason"] = (
                f"Composite score {score:.2f} >= entry threshold {ENTRY_THRESHOLD} "
                f"(trend/momentum/vol/structure)"
            )
        elif score <= EXIT_THRESHOLD:
            result["exit"]   = True
            result["side"]   = "sell"
            result["reason"] = (
                f"Composite score {score:.2f} <= exit threshold {EXIT_THRESHOLD} "
                f"(trend/momentum/vol/structure)"
            )

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

    # ── Trailing drawdown tracking ──────────────────────────────────────────
    peak_balance = account.get("peak_balance", account.get("initial_balance", DEFAULT_BALANCE))
    current_balance = account.get("account_balance", DEFAULT_BALANCE)
    if current_balance > peak_balance:
        peak_balance = current_balance
    account["peak_balance"] = peak_balance

    trailing_floor = peak_balance * (1 - TRAILING_DD_PCT / 100)
    trailing_dd_current = round((peak_balance - current_balance) / peak_balance * 100, 2) if peak_balance > 0 else 0

    if current_balance <= trailing_floor and TRAILING_DD_PCT > 0:
        # FORCE LIQUIDATE ALL POSITIONS
        for pos in list(account.get("open_positions", [])):
            close_price = pos.get("entry_price", current_balance)  # best effort
            pnl = 0  # approximate — real price unknown without fetch
            account["trade_log"].append({
                "type": "exit", "side": "liquidation", "symbol": pos["symbol"],
                "filled_price": close_price, "qty": pos.get("qty", 1),
                "reason": "trailing_drawdown_breach", "pnl": pnl,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
        account["open_positions"] = []
        account["trading_halted_reason"] = "trailing_drawdown_breach"
        save_virtual_account(account)
        log.critical("TRAILING DRAWDOWN BREACHED: $%.2f <= floor $%.2f (peak $%.2f)", current_balance, trailing_floor, peak_balance)
        return {
            "status": "halted", "trades_made": 0, "open_positions": 0,
            "account_balance": current_balance, "peak_balance": peak_balance,
            "trailing_dd_current": trailing_dd_current, "trailing_dd_limit": TRAILING_DD_PCT,
            "summary": f"TRAILING DRAWDOWN BREACH — all positions liquidated. Balance ${current_balance:.2f} hit floor ${trailing_floor:.2f}",
            "escalate": True, "escalation_reason": "trailing_drawdown_breach",
            "errors": [],
        }

    if cycle_state is None:
        state_file = AGENT_NETWORK_STATE / "spx500_cycle_state.json"
        cycle_state = _load_file(state_file)

    strategy_id = "Bollinger Lower Band Touch with ATR Expansion Confirmation"
    timeframe   = "1d"   # fallback — daily candles
    if cycle_state:
        strat       = cycle_state.get("active_strategy", {})
        strategy_id = (
            strat.get("strategy_name")
            or strat.get("strategy_id")
            or strategy_id
        )
        # Read timeframe from the strategy schema (15m, 1h, 4h, 1d)
        schema_tf = (
            strat.get("strategy_schema", {})
                 .get("metadata", {})
                 .get("timeframe", "1d")
        )
        if schema_tf in _TF_MAP:
            timeframe = schema_tf
    log.info("Virtual trader running on %s timeframe (strategy: %s)", timeframe, strategy_id)

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
            log.warning("CIRCUIT BREAKER: daily loss limit hit (%.2f)", daily_pnl)
            # Force-close all open positions
            for pos in list(account["open_positions"]):
                account["trade_log"].append({
                    "type": "exit", "side": "liquidation", "symbol": pos["symbol"],
                    "filled_price": pos.get("entry_price", 0), "qty": pos.get("qty", 1),
                    "reason": "daily_loss_halt_liquidation", "pnl": 0,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
            account["open_positions"] = []
            log.warning("DAILY LOSS HALT: all positions force-closed")
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

    for display_name, ticker in INSTRUMENTS.items():
        try:
            if trading_halted:
                log.warning("Trading halted (circuit breaker) — skipping %s", display_name)
                continue

            ohlcv = fetch_ohlcv(ticker, timeframe)
            if not ohlcv:
                errors.append(f"No OHLCV data for {display_name} ({ticker})")
                continue

            # Pass strategy schema so signals use backtested parameters
            _schema = cycle_state.get("active_strategy", {}).get("strategy_schema") if cycle_state else None
            signals = get_strategy_signals(strategy_id, ohlcv, strategy_schema=_schema)
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
                    log.info("CONTRACT LIMIT: total %d >= max %d — skipping entry", current_contracts, MAX_TOTAL_CONTRACTS)
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
                qty = min(qty, MAX_CONTRACTS_PER_INSTRUMENT)
                qty = min(qty, MAX_TOTAL_CONTRACTS - current_contracts)
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

    # ── Update trailing drawdown peak after trades ────────────────────────
    end_balance = account.get("account_balance", DEFAULT_BALANCE)
    if end_balance > account.get("peak_balance", 0):
        account["peak_balance"] = end_balance
    peak_balance = account["peak_balance"]
    trailing_dd_current = round((peak_balance - end_balance) / peak_balance * 100, 2) if peak_balance > 0 else 0

    save_virtual_account(account)

    status = "success" if not errors else ("partial" if trades_made or not errors else "failed")
    if errors and not trades_made:
        status = "failed"

    return {
        "status":           status,
        "trades_made":      len(trades_made),
        "open_positions":   len(account["open_positions"]),
        "account_balance":  account["account_balance"],
        "peak_balance":     peak_balance,
        "trailing_dd_current": trailing_dd_current,
        "trailing_dd_limit":   TRAILING_DD_PCT,
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
