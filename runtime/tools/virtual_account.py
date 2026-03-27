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

# Known pairwise correlations (approximate, based on historical data)
# High correlation = potential double-exposure risk
INSTRUMENT_CORRELATIONS = {
    ("SPX500", "NAS100"): 0.92,
    ("SPX500", "US30"):   0.88,
    ("SPX500", "XAUUSD"): -0.15,
    ("NAS100", "US30"):   0.90,
    ("NAS100", "XAUUSD"): -0.12,
    ("US30",   "XAUUSD"): -0.10,
}
MAX_PORTFOLIO_CORRELATION = 0.80


def _correlation(inst_a: str, inst_b: str) -> float:
    key = (inst_a, inst_b) if (inst_a, inst_b) in INSTRUMENT_CORRELATIONS else (inst_b, inst_a)
    return INSTRUMENT_CORRELATIONS.get(key, 0.0)


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
                if side == "buy":
                    fill_price = round(current_price * (1 - SLIPPAGE_BPS / 10000), 4)
                else:
                    fill_price = round(current_price * (1 + SLIPPAGE_BPS / 10000), 4)

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
                if signals["side"] == "buy":
                    fill_price = round(current_price * (1 + SLIPPAGE_BPS / 10000), 4)
                else:
                    fill_price = round(current_price * (1 - SLIPPAGE_BPS / 10000), 4)

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
