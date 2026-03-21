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

# Instruments: display_name -> yfinance ticker
INSTRUMENTS = {
    "SPX500": "^GSPC",
    "GOLD":   "GC=F",
}

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
                         auto_adjust=True, progress=False)
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
            strat.get("strategy_id")
            or strat.get("strategy_name")
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

    now         = datetime.now(timezone.utc)
    balance     = account.get("account_balance", DEFAULT_BALANCE)
    risk_usd    = balance * (account.get("risk_per_trade_pct", RISK_PER_TRADE_PCT) / 100)
    trades_made = []
    errors      = []

    for display_name, ticker in INSTRUMENTS.items():
        try:
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

                pnl = (
                    (current_price - entry_price) * qty
                    if side == "buy"
                    else (entry_price - current_price) * qty
                )
                r_multiple = round(pnl / entry_risk, 2) if entry_risk else 0.0

                stop_reason = "Stop loss triggered" if _stop_hit(open_pos, current_price) else ""
                exit_record = {
                    "order_id":    open_pos["order_id"],
                    "type":        "exit",
                    "strategy_id": strategy_id,
                    "side":        "sell" if side == "buy" else "buy",
                    "symbol":      display_name,
                    "filled_price": current_price,
                    "qty":         qty,
                    "risk_usd":    entry_risk,
                    "reason":      stop_reason or signals["reason"] or "Exit signal",
                    "pnl":         round(pnl, 2),
                    "r_multiple":  r_multiple,
                    "timestamp":   now.isoformat(),
                }
                account["trade_log"].append(exit_record)
                account["open_positions"] = [
                    p for p in account["open_positions"]
                    if p["symbol"] != display_name
                ]
                balance = round(balance + pnl, 2)
                account["account_balance"] = balance
                risk_usd = balance * (account.get("risk_per_trade_pct", RISK_PER_TRADE_PCT) / 100)
                trades_made.append(exit_record)
                log.info("CLOSE %s @ %.2f | PnL=%.2f R=%.2f",
                         display_name, current_price, pnl, r_multiple)

            # ── Open new position ────────────────────────────────────────────
            elif not open_pos and signals["entry"] and current_price > 0:
                qty      = max(1, int(risk_usd / (current_price * STOP_PCT)))
                order_id = f"VA-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
                stop     = (
                    round(current_price * (1 - STOP_PCT), 2)
                    if signals["side"] == "buy"
                    else round(current_price * (1 + STOP_PCT), 2)
                )

                entry_record = {
                    "order_id":    order_id,
                    "type":        "entry",
                    "strategy_id": strategy_id,
                    "side":        signals["side"],
                    "symbol":      display_name,
                    "filled_price": current_price,
                    "qty":         qty,
                    "risk_usd":    round(risk_usd, 2),
                    "reason":      signals["reason"],
                    "pnl":         None,
                    "r_multiple":  None,
                    "timestamp":   now.isoformat(),
                }
                account["trade_log"].append(entry_record)
                account["open_positions"].append({
                    "order_id":   order_id,
                    "symbol":     display_name,
                    "side":       signals["side"],
                    "entry_price": current_price,
                    "qty":        qty,
                    "risk_usd":   round(risk_usd, 2),
                    "stop_loss":  stop,
                    "opened_at":  now.isoformat(),
                })
                trades_made.append(entry_record)
                log.info("OPEN %s %s @ %.2f | qty=%d risk=%.2f",
                         signals["side"].upper(), display_name, current_price, qty, risk_usd)

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
