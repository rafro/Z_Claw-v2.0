"""
Alpaca Markets market data provider.
Free tier provides unlimited 1-minute history for US stocks and ETFs.
Futures symbols are mapped to ETF proxies (SPY, QQQ, GLD, etc.).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Futures -> ETF proxy mapping ─────────────────────────────────────────────
FUTURES_TO_ETF = {
    "MES": "SPY",   "SPX500": "SPY",
    "MNQ": "QQQ",   "NAS100": "QQQ",
    "MGC": "GLD",   "XAUUSD": "GLD",
    "MYM": "DIA",   "US30":   "DIA",
    "MCL": "USO",   "CRUDE":  "USO",
    "MBT": "TLT",   "BONDS":  "TLT",
}

# ── Timeframe -> Alpaca timeframe string ─────────────────────────────────────
_TF_MAP = {
    "1m":  "1Min",
    "5m":  "5Min",
    "15m": "15Min",
    "1h":  "1Hour",
    "4h":  "1Hour",   # fetch 1h, resample to 4h
    "1d":  "1Day",
}

BASE_URL = "https://data.alpaca.markets/v2/stocks"
MAX_BARS_PER_REQUEST = 10000

# ── Session definitions (UTC boundaries) ─────────────────────────────────────
SESSION_RANGES = {
    "rth":      (14, 30, 21, 0),   # 9:30-16:00 ET -> 14:30-21:00 UTC
    "ny_rth":   (14, 30, 21, 0),
    "extended": (13, 0,  22, 0),   # 8:00-17:00 ET -> 13:00-22:00 UTC
    "london":   (8,  0,  16, 30),
    "asia":     (0,  0,  8,  0),
}


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
    return {
        "ticker": ohlcv["ticker"],
        "date": r_d, "open": r_o, "high": r_h,
        "low": r_l, "close": r_c, "volume": r_v,
    }


def _filter_session(bars: list[dict], session: str) -> list[dict]:
    """Filter raw Alpaca bar dicts by session time boundaries."""
    if session not in SESSION_RANGES:
        return bars
    start_h, start_m, end_h, end_m = SESSION_RANGES[session]
    filtered = []
    for bar in bars:
        ts = bar["t"]
        # Alpaca timestamps are ISO-8601, e.g. "2024-06-15T14:30:00Z"
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        h, m = dt.hour, dt.minute
        after_start = (h > start_h) or (h == start_h and m >= start_m)
        before_end = (h < end_h) or (h == end_h and m < end_m)
        if after_start and before_end:
            filtered.append(bar)
    return filtered


class AlpacaProvider(MarketDataProvider):
    """Free stock/ETF market data via Alpaca Markets API."""

    provider_id: str = "alpaca"

    def __init__(self) -> None:
        self._api_key = os.getenv("ALPACA_API_KEY", "")
        self._secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    def is_available(self) -> bool:
        """Check API keys are set and a test request succeeds."""
        if not self._api_key or not self._secret_key:
            return False
        try:
            resp = requests.get(
                f"{BASE_URL}/SPY/bars",
                headers=self._headers(),
                params={"timeframe": "1Day", "limit": "1"},
                timeout=10,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return ["*"]  # any US stock/ETF ticker

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": self._api_key,
            "APCA-API-SECRET-KEY": self._secret_key,
        }

    def _resolve_ticker(self, symbol: str) -> str:
        """Map futures/alias symbols to ETF proxies."""
        return FUTURES_TO_ETF.get(symbol.upper(), symbol.upper())

    def _fetch_bars_paginated(
        self,
        ticker: str,
        alpaca_tf: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> list[dict]:
        """Fetch all bars with pagination (Alpaca max 10,000 per request)."""
        all_bars: list[dict] = []
        page_token: Optional[str] = None

        params: dict = {
            "timeframe": alpaca_tf,
            "limit": str(MAX_BARS_PER_REQUEST),
            "adjustment": "raw",
        }
        if start:
            params["start"] = start
        if end:
            params["end"] = end

        while True:
            if page_token:
                params["page_token"] = page_token

            resp = requests.get(
                f"{BASE_URL}/{ticker}/bars",
                headers=self._headers(),
                params=params,
                timeout=30,
            )

            if resp.status_code != 200:
                log.error(
                    "Alpaca API error %d for %s: %s",
                    resp.status_code, ticker, resp.text[:200],
                )
                break

            data = resp.json()
            bars = data.get("bars", [])
            if not bars:
                break

            all_bars.extend(bars)
            page_token = data.get("next_page_token")
            if not page_token:
                break

        return all_bars

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV via Alpaca Markets API.

        Handles pagination automatically. Futures symbols are mapped to
        ETF proxies (e.g., MES -> SPY).
        """
        if not self._api_key or not self._secret_key:
            log.error("Alpaca API keys not configured")
            return None

        ticker = self._resolve_ticker(symbol)
        alpaca_tf = _TF_MAP.get(timeframe)
        if not alpaca_tf:
            log.error("Alpaca: unsupported timeframe '%s'", timeframe)
            return None

        # For 4h, fetch 1h and resample
        fetch_tf = "1Hour" if timeframe == "4h" else alpaca_tf

        # Default start to 60 days ago if not specified
        if not start:
            from datetime import timedelta
            start = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).strftime("%Y-%m-%d")

        try:
            bars = self._fetch_bars_paginated(ticker, fetch_tf, start, end)
            if not bars:
                log.warning("Alpaca: no data returned for %s (%s)", symbol, ticker)
                return None

            # Apply session filter before converting
            if session != "all" and timeframe in ("1m", "5m", "15m", "1h", "4h"):
                bars = _filter_session(bars, session)
                if not bars:
                    log.warning(
                        "Alpaca: no bars after session filter '%s' for %s",
                        session, ticker,
                    )
                    return None

            ohlcv = {
                "ticker": ticker,
                "date":   [bar["t"] for bar in bars],
                "open":   [float(bar["o"]) for bar in bars],
                "high":   [float(bar["h"]) for bar in bars],
                "low":    [float(bar["l"]) for bar in bars],
                "close":  [float(bar["c"]) for bar in bars],
                "volume": [float(bar["v"]) for bar in bars],
            }

            if timeframe == "4h":
                ohlcv = _resample_4h(ohlcv)

            log.debug(
                "Alpaca: fetched %d %s bars for %s (session=%s)",
                len(ohlcv["close"]), timeframe, ticker, session,
            )
            return ohlcv

        except Exception as e:
            log.error("Alpaca fetch failed for %s: %s", ticker, e)
            return None
