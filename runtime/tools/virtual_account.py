"""
Virtual account manager — diversified paper trading with real yfinance data.
Instruments: SPX500, XAUUSD, CRUDE, BONDS (loaded from assets.json).
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
SLIPPAGE_BPS       = 5     # default fallback: 5 basis points per fill (0.05%)
INSTRUMENT_SLIPPAGE_BPS = {
    "SPX500": 3,
    "XAUUSD": 8,
    "CRUDE":  5,
    "BONDS":  3,
}
DAILY_LOSS_HALT_PCT = 3.0  # halt if daily PnL < -3% of account
STREAK_HALT_COUNT   = 5    # halt after N consecutive losses

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


def _slippage_bps(instrument: str) -> int:
    """Per-instrument slippage in basis points; falls back to SLIPPAGE_BPS (5)."""
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


def _calc_rsi(prices: list, period: int = 14) -> Optional[float]:
    """Return the latest RSI value (0-100) or None if insufficient data."""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    # Seed with SMA then EMA-smooth
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_rsi_series(prices: list, period: int = 14) -> list:
    """Return full RSI series aligned with *prices* (None-padded at the front)."""
    result = [None] * min(period, len(prices))
    if len(prices) < period + 1:
        return [None] * len(prices)
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss else 999
    result.append(100 - (100 / (1 + rs)))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss else 999
        result.append(100 - (100 / (1 + rs)))
    return result


def _calc_macd(prices: list, fast: int = 12, slow: int = 26,
               signal: int = 9) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Return (macd_line, signal_line, histogram) for the latest bar."""
    ema_fast = _calc_ema(prices, fast)
    ema_slow = _calc_ema(prices, slow)
    if _last(ema_fast) is None or _last(ema_slow) is None:
        return None, None, None
    macd_line_series = []
    for f, s in zip(ema_fast, ema_slow):
        macd_line_series.append(f - s if f is not None and s is not None else None)
    valid_macd = [v for v in macd_line_series if v is not None]
    if len(valid_macd) < signal:
        return _last(macd_line_series), None, None
    signal_series = _calc_ema(valid_macd, signal)
    macd_val = valid_macd[-1]
    signal_val = _last(signal_series)
    histogram = macd_val - signal_val if signal_val is not None else None
    return macd_val, signal_val, histogram


def _calc_di(high: list, low: list, close: list, period: int = 14) -> tuple[list, list]:
    """Return (+DI, -DI) series."""
    plus_dm, minus_dm = [], []
    for i in range(1, len(close)):
        up_move = high[i] - high[i - 1]
        down_move = low[i - 1] - low[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)
    atr_series = _calc_atr(high, low, close, period)
    sm_plus = _calc_ema(plus_dm, period)
    sm_minus = _calc_ema(minus_dm, period)
    plus_di, minus_di = [None], [None]  # offset by 1 for alignment
    for i in range(len(sm_plus)):
        atr_val = atr_series[i + 1] if (i + 1) < len(atr_series) else None
        if sm_plus[i] is not None and atr_val and atr_val > 0:
            plus_di.append(100 * sm_plus[i] / atr_val)
        else:
            plus_di.append(None)
        if sm_minus[i] is not None and atr_val and atr_val > 0:
            minus_di.append(100 * sm_minus[i] / atr_val)
        else:
            minus_di.append(None)
    return plus_di, minus_di


def _calc_adx(high: list, low: list, close: list, period: int = 14) -> Optional[float]:
    """Return latest ADX value (0-100)."""
    plus_di, minus_di = _calc_di(high, low, close, period)
    dx_series = []
    for p, m in zip(plus_di, minus_di):
        if p is not None and m is not None and (p + m) > 0:
            dx_series.append(100 * abs(p - m) / (p + m))
    if len(dx_series) < period:
        return None
    adx_vals = _calc_ema(dx_series, period)
    return _last(adx_vals)


def _calc_vwap(high: list, low: list, close: list, volume: list) -> Optional[float]:
    """Return cumulative VWAP for the available data. Returns latest value."""
    if not close or not volume:
        return None
    cum_vol = 0.0
    cum_tp_vol = 0.0
    for h, l, c, v in zip(high, low, close, volume):
        tp = (h + l + c) / 3
        cum_vol += v
        cum_tp_vol += tp * v
    if cum_vol == 0:
        return None
    return cum_tp_vol / cum_vol


def _calc_stochastic(high: list, low: list, close: list,
                     k_period: int = 14, d_period: int = 3) -> tuple[Optional[float], Optional[float]]:
    """Return (%K, %D) for the latest bar."""
    if len(close) < k_period:
        return None, None
    k_series = []
    for i in range(k_period - 1, len(close)):
        window_high = max(high[i - k_period + 1: i + 1])
        window_low = min(low[i - k_period + 1: i + 1])
        if window_high == window_low:
            k_series.append(50.0)
        else:
            k_series.append(100 * (close[i] - window_low) / (window_high - window_low))
    if not k_series:
        return None, None
    pct_k = k_series[-1]
    if len(k_series) >= d_period:
        pct_d = sum(k_series[-d_period:]) / d_period
    else:
        pct_d = pct_k
    return pct_k, pct_d


def _detect_structure(high: list, low: list, close: list) -> str:
    """
    Detect market structure: 'uptrend', 'downtrend', or 'range'.
    Uses higher-highs/higher-lows vs lower-highs/lower-lows over last 20 bars.
    """
    lookback = min(20, len(close))
    if lookback < 6:
        return "range"
    recent_high = high[-lookback:]
    recent_low = low[-lookback:]
    # Split into 4 segments and check swing progression
    seg = lookback // 4
    seg_highs = [max(recent_high[i * seg:(i + 1) * seg]) for i in range(4)]
    seg_lows = [min(recent_low[i * seg:(i + 1) * seg]) for i in range(4)]
    hh_count = sum(1 for i in range(1, 4) if seg_highs[i] > seg_highs[i - 1])
    hl_count = sum(1 for i in range(1, 4) if seg_lows[i] > seg_lows[i - 1])
    lh_count = sum(1 for i in range(1, 4) if seg_highs[i] < seg_highs[i - 1])
    ll_count = sum(1 for i in range(1, 4) if seg_lows[i] < seg_lows[i - 1])
    if hh_count >= 2 and hl_count >= 2:
        return "uptrend"
    if lh_count >= 2 and ll_count >= 2:
        return "downtrend"
    return "range"


def _detect_regime(close: list, atr: list) -> str:
    """
    Detect volatility regime: 'low_vol', 'normal', 'high_vol'.
    Compares recent ATR% to longer lookback median.
    """
    valid_atr = [v for v in atr if v is not None]
    if len(valid_atr) < 20 or close[-1] <= 0:
        return "normal"
    recent_atr_pct = valid_atr[-1] / close[-1]
    lookback = valid_atr[-50:] if len(valid_atr) >= 50 else valid_atr
    median_idx = len(lookback) // 2
    sorted_lb = sorted(lookback)
    median_atr = sorted_lb[median_idx]
    median_pct = median_atr / close[-1] if close[-1] > 0 else 0
    if median_pct == 0:
        return "normal"
    ratio = recent_atr_pct / median_pct
    if ratio > 1.5:
        return "high_vol"
    if ratio < 0.6:
        return "low_vol"
    return "normal"


# ── Multi-factor scoring system ───────────────────────────────────────────────

SIGNAL_WEIGHTS = {
    "trend":      0.30,
    "momentum":   0.25,
    "volatility": 0.20,
    "volume":     0.10,
    "structure":  0.15,
}

ENTRY_THRESHOLD = 0.60   # composite score >= 0.60 to enter
EXIT_THRESHOLD  = 0.35   # composite score <= 0.35 to exit


def _score_trend(close: list, high: list, low: list) -> float:
    """Score trend alignment 0.0-1.0. Combines EMA stack + ADX."""
    score = 0.0
    ema20 = _last(_calc_ema(close, 20))
    ema50 = _last(_calc_ema(close, 50))
    price = close[-1]
    if ema20 is None or ema50 is None:
        return 0.5
    # EMA ordering
    if price > ema20 > ema50:
        score += 0.6
    elif price > ema20:
        score += 0.4
    elif price < ema20 < ema50:
        score += 0.0
    else:
        score += 0.2
    # ADX strength
    adx = _calc_adx(high, low, close)
    if adx is not None:
        if adx > 25:
            score += 0.4
        elif adx > 15:
            score += 0.2
        else:
            score += 0.1
    else:
        score += 0.2
    return min(score, 1.0)


def _score_momentum(close: list, high: list, low: list) -> float:
    """Score momentum 0.0-1.0. Combines RSI + MACD + Stochastic."""
    components = []
    # RSI
    rsi = _calc_rsi(close)
    if rsi is not None:
        if 40 <= rsi <= 70:
            components.append(0.8)  # healthy bullish
        elif 30 <= rsi < 40:
            components.append(0.5)  # neutral
        elif rsi > 70:
            components.append(0.3)  # overbought
        elif rsi < 30:
            components.append(0.4)  # oversold (potential bounce)
        else:
            components.append(0.2)
    # MACD
    macd_val, signal_val, hist = _calc_macd(close)
    if hist is not None:
        if hist > 0:
            components.append(0.8)
        else:
            components.append(0.2)
    # Stochastic
    pct_k, pct_d = _calc_stochastic(high, low, close)
    if pct_k is not None and pct_d is not None:
        if pct_k > pct_d and pct_k < 80:
            components.append(0.8)
        elif pct_k > pct_d:
            components.append(0.5)
        else:
            components.append(0.3)
    if not components:
        return 0.5
    return sum(components) / len(components)


def _score_volatility(close: list, high: list, low: list, atr: list) -> float:
    """Score volatility conditions 0.0-1.0. Prefers expanding but not extreme."""
    score = 0.5
    expanding = _atr_expanding(atr)
    if expanding is True:
        score += 0.2
    elif expanding is False:
        score -= 0.1
    regime = _detect_regime(close, atr)
    if regime == "normal":
        score += 0.2
    elif regime == "high_vol":
        score -= 0.2
    elif regime == "low_vol":
        score += 0.1  # low vol can precede breakouts
    # Bollinger width
    bb_upper, bb_mid, bb_lower = _calc_bollinger(close)
    u = _last(bb_upper)
    l = _last(bb_lower)
    m = _last(bb_mid)
    if u is not None and l is not None and m and m > 0:
        bb_width = (u - l) / m
        if 0.02 < bb_width < 0.08:
            score += 0.1  # moderate bandwidth
    return max(0.0, min(1.0, score))


def _score_volume(close: list, volume: list, high: list, low: list) -> float:
    """Score volume confirmation 0.0-1.0."""
    if not volume or len(volume) < 20:
        return 0.5
    recent_vol = volume[-5:]
    avg_vol = sum(volume[-20:]) / 20
    if avg_vol == 0:
        return 0.5
    vol_ratio = (sum(recent_vol) / len(recent_vol)) / avg_vol
    score = 0.5
    if vol_ratio > 1.3:
        score += 0.3  # above-average volume confirms move
    elif vol_ratio > 1.0:
        score += 0.1
    else:
        score -= 0.1
    # VWAP position
    vwap = _calc_vwap(high, low, close, volume)
    if vwap is not None and close[-1] > vwap:
        score += 0.2
    return max(0.0, min(1.0, score))


def _score_structure(high: list, low: list, close: list) -> float:
    """Score market structure 0.0-1.0."""
    structure = _detect_structure(high, low, close)
    if structure == "uptrend":
        return 0.85
    elif structure == "downtrend":
        return 0.15
    return 0.50


def _composite_score(ohlcv: dict) -> tuple[float, dict]:
    """
    Compute weighted composite score across all factors.
    Returns (score, detail_dict).
    """
    close = ohlcv["close"]
    high = ohlcv["high"]
    low = ohlcv["low"]
    volume = ohlcv["volume"]
    atr = _calc_atr(high, low, close)

    trend = _score_trend(close, high, low)
    momentum = _score_momentum(close, high, low)
    volatility = _score_volatility(close, high, low, atr)
    vol = _score_volume(close, volume, high, low)
    structure = _score_structure(high, low, close)

    composite = (
        trend * SIGNAL_WEIGHTS["trend"]
        + momentum * SIGNAL_WEIGHTS["momentum"]
        + volatility * SIGNAL_WEIGHTS["volatility"]
        + vol * SIGNAL_WEIGHTS["volume"]
        + structure * SIGNAL_WEIGHTS["structure"]
    )
    detail = {
        "trend": round(trend, 3),
        "momentum": round(momentum, 3),
        "volatility": round(volatility, 3),
        "volume": round(vol, 3),
        "structure": round(structure, 3),
        "composite": round(composite, 3),
    }
    return composite, detail


# ── Trailing stop ─────────────────────────────────────────────────────────────

def _check_trailing_stop(position: dict, current_price: float,
                         atr_value: Optional[float]) -> Optional[float]:
    """
    Update trailing stop based on ATR. Returns new stop level or None.
    Uses 2x ATR as trailing distance.
    """
    if atr_value is None or atr_value <= 0:
        return None
    side = position.get("side", "buy")
    current_stop = position.get("stop_loss")
    trail_distance = 2.0 * atr_value

    if side == "buy":
        new_stop = round(current_price - trail_distance, 2)
        if current_stop is None or new_stop > current_stop:
            return new_stop
    else:
        new_stop = round(current_price + trail_distance, 2)
        if current_stop is None or new_stop < current_stop:
            return new_stop
    return None


def _atr_expanding(atr: list, lookback: int = 5) -> Optional[bool]:
    valid = [v for v in atr if v is not None]
    if len(valid) < lookback + 1:
        return None
    current    = valid[-1]
    recent_avg = sum(valid[-(lookback + 1) : -1]) / lookback
    return current > recent_avg


def _last(values: list) -> Optional[float]:
    return next((v for v in reversed(values) if v is not None), None)


# ── Signal engine ──────────────────────────────────────────────────────────────

def get_strategy_signals(strategy_id: str, ohlcv: dict) -> dict:
    """
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

    # ── Multi-factor composite scoring fallback ──────────────────────────────
    else:
        composite, detail = _composite_score(ohlcv)
        structure = _detect_structure(high, low, close)
        side = "buy" if structure != "downtrend" else "sell"
        if composite >= ENTRY_THRESHOLD:
            result["entry"] = True
            result["side"]  = side
            result["reason"] = (
                f"Multi-factor composite {composite:.2f} >= {ENTRY_THRESHOLD} "
                f"(trend={detail['trend']:.2f} mom={detail['momentum']:.2f} "
                f"vol={detail['volatility']:.2f} struct={detail['structure']:.2f})"
            )
        elif composite <= EXIT_THRESHOLD:
            result["exit"]   = True
            result["reason"] = (
                f"Multi-factor composite {composite:.2f} <= {EXIT_THRESHOLD} "
                f"(trend={detail['trend']:.2f} mom={detail['momentum']:.2f} "
                f"vol={detail['volatility']:.2f} struct={detail['structure']:.2f})"
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

            signals       = get_strategy_signals(strategy_id, ohlcv)
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
                slip = _slippage_bps(display_name)
                if side == "buy":
                    fill_price = round(current_price * (1 - slip / 10000), 4)
                else:
                    fill_price = round(current_price * (1 + slip / 10000), 4)

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
                slip = _slippage_bps(display_name)
                if signals["side"] == "buy":
                    fill_price = round(current_price * (1 + slip / 10000), 4)
                else:
                    fill_price = round(current_price * (1 - slip / 10000), 4)

                qty      = max(1, int(adjusted_risk_usd / (fill_price * STOP_PCT)))
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

            # ── Update trailing stop for held positions ───────────────────────
            elif open_pos and not signals["exit"] and not _stop_hit(open_pos, current_price):
                atr_values = _calc_atr(ohlcv["high"], ohlcv["low"], ohlcv["close"])
                current_atr = next((v for v in reversed(atr_values) if v is not None), None)
                new_stop = _check_trailing_stop(open_pos, current_price, current_atr)
                if new_stop is not None:
                    old_stop = open_pos.get("stop_loss")
                    open_pos["stop_loss"] = new_stop
                    log.info("TRAIL %s stop: %.2f → %.2f (ATR=%.2f)",
                             display_name, old_stop or 0, new_stop, current_atr or 0)

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
