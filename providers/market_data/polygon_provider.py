"""
Polygon.io market data provider — stocks, ETFs, and CME futures data.
Works internationally (no geo-restrictions like Alpaca).
Free tier: 5 API calls/minute. Paid: unlimited.
Supports actual futures contracts AND ETF proxies.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Futures ticker mapping (Polygon format) ──────────────────────────────────
# Polygon uses plain tickers for continuous front-month contracts.
# For specific expiries, use e.g. "MESM2025" — but continuous is fine for signals.
FUTURES_MAP = {
    "MES": "MES",
    "MNQ": "MNQ",
    "MGC": "MGC",
    "MYM": "MYM",
    "MCL": "MCL",
    "MBT": "MBT",
}

# ── Futures/alias -> ETF proxy (fallback if futures ticker fails) ────────────
ETF_PROXY_MAP = {
    "MES": "SPY",   "SPX500": "SPY",
    "MNQ": "QQQ",   "NAS100": "QQQ",
    "MGC": "GLD",   "XAUUSD": "GLD",
    "MYM": "DIA",   "US30":   "DIA",
    "MCL": "USO",   "CRUDE":  "USO",
    "MBT": "TLT",   "BONDS":  "TLT",
}

# ── Timeframe -> (multiplier, timespan) ──────────────────────────────────────
_TF_MAP: dict[str, tuple[int, str]] = {
    "1m":  (1,  "minute"),
    "5m":  (5,  "minute"),
    "15m": (15, "minute"),
    "1h":  (1,  "hour"),
    "4h":  (1,  "hour"),   # fetch 1h, resample to 4h
    "1d":  (1,  "day"),
}

BASE_URL = "https://api.polygon.io"
MAX_RESULTS_PER_REQUEST = 50000

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
    """Filter raw bar dicts by session time boundaries (UTC)."""
    if session not in SESSION_RANGES:
        return bars
    start_h, start_m, end_h, end_m = SESSION_RANGES[session]
    filtered = []
    for bar in bars:
        # Polygon timestamps are milliseconds since epoch
        dt = datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc)
        h, m = dt.hour, dt.minute
        after_start = (h > start_h) or (h == start_h and m >= start_m)
        before_end = (h < end_h) or (h == end_h and m < end_m)
        if after_start and before_end:
            filtered.append(bar)
    return filtered


class PolygonProvider(MarketDataProvider):
    """Polygon.io market data — stocks, ETFs, and CME futures. Works internationally."""

    provider_id: str = "polygon"

    def __init__(self) -> None:
        self._api_key = os.getenv("POLYGON_API_KEY", "")

    def is_available(self) -> bool:
        """Check API key is set and a test request succeeds."""
        if not self._api_key:
            return False
        try:
            resp = requests.get(
                f"{BASE_URL}/v2/aggs/ticker/SPY/range/1/day/"
                f"{(datetime.now(timezone.utc) - timedelta(days=7)).strftime('%Y-%m-%d')}/"
                f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
                params={"apiKey": self._api_key, "limit": "1"},
                timeout=10,
            )
            if resp.status_code == 429:
                # Rate limited but key is valid
                log.warning("Polygon: rate limited on availability check (free tier)")
                return True
            return resp.status_code == 200
        except Exception:
            return False

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return ["*"]  # any US stock/ETF/futures ticker

    def _resolve_ticker(self, symbol: str) -> tuple[str, bool]:
        """
        Map symbol to Polygon ticker.

        Returns:
            (ticker, is_futures) — is_futures=True means we should fall back
            to ETF proxy if the futures ticker returns no data.
        """
        upper = symbol.upper()
        if upper in FUTURES_MAP:
            return FUTURES_MAP[upper], True
        if upper in ETF_PROXY_MAP:
            return ETF_PROXY_MAP[upper], False
        return upper, False

    def _fetch_bars_paginated(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        start: str,
        end: str,
    ) -> Optional[list[dict]]:
        """
        Fetch all bars with pagination.
        Polygon returns max 50,000 results per request; use next_url for more.
        Returns None on rate limit (429), empty list on other errors.
        """
        all_bars: list[dict] = []
        url = (
            f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/"
            f"{multiplier}/{timespan}/{start}/{end}"
        )
        params: dict = {
            "apiKey": self._api_key,
            "limit": str(MAX_RESULTS_PER_REQUEST),
            "sort": "asc",
        }

        while url:
            resp = requests.get(url, params=params, timeout=30)

            if resp.status_code == 429:
                log.warning(
                    "Polygon: rate limited (429) fetching %s — "
                    "free tier is 5 calls/min. Skipping.",
                    ticker,
                )
                return None

            if resp.status_code != 200:
                log.error(
                    "Polygon API error %d for %s: %s",
                    resp.status_code, ticker, resp.text[:200],
                )
                break

            data = resp.json()
            results = data.get("results", [])
            if not results:
                break

            all_bars.extend(results)

            # Polygon provides next_url for pagination (already includes apiKey)
            next_url = data.get("next_url")
            if next_url:
                # next_url is a full URL; clear params so we don't double-add
                url = next_url
                params = {"apiKey": self._api_key}
            else:
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
        Fetch OHLCV via Polygon.io API.

        Handles pagination automatically. Futures symbols are tried first;
        if no data, falls back to ETF proxy. Rate limit (429) returns None
        to let the fallback chain handle it.
        """
        if not self._api_key:
            log.error("Polygon API key not configured")
            return None

        tf_entry = _TF_MAP.get(timeframe)
        if not tf_entry:
            log.error("Polygon: unsupported timeframe '%s'", timeframe)
            return None

        multiplier, timespan = tf_entry
        # For 4h, fetch 1h and resample
        if timeframe == "4h":
            multiplier, timespan = 1, "hour"

        # Default date range
        now = datetime.now(timezone.utc)
        if not end:
            end = now.strftime("%Y-%m-%d")
        if not start:
            if timespan == "day":
                start = (now - timedelta(days=365)).strftime("%Y-%m-%d")
            else:
                start = (now - timedelta(days=30)).strftime("%Y-%m-%d")

        ticker, is_futures = self._resolve_ticker(symbol)

        try:
            bars = self._fetch_bars_paginated(
                ticker, multiplier, timespan, start, end,
            )

            # Rate limited — return None, let fallback chain handle it
            if bars is None:
                return None

            # If futures ticker returned no data, try ETF proxy
            if not bars and is_futures:
                etf_ticker = ETF_PROXY_MAP.get(symbol.upper())
                if etf_ticker:
                    log.info(
                        "Polygon: no futures data for %s, falling back to ETF proxy %s",
                        ticker, etf_ticker,
                    )
                    ticker = etf_ticker
                    bars = self._fetch_bars_paginated(
                        ticker, multiplier, timespan, start, end,
                    )
                    if bars is None:
                        return None

            if not bars:
                log.warning("Polygon: no data returned for %s (%s)", symbol, ticker)
                return None

            # Apply session filter before converting
            if session != "all" and timeframe in ("1m", "5m", "15m", "1h", "4h"):
                bars = _filter_session(bars, session)
                if not bars:
                    log.warning(
                        "Polygon: no bars after session filter '%s' for %s",
                        session, ticker,
                    )
                    return None

            # Convert Polygon response to standard OHLCV dict
            # Polygon fields: t=timestamp_ms, o=open, h=high, l=low, c=close, v=volume
            ohlcv = {
                "ticker": ticker,
                "date":   [
                    datetime.fromtimestamp(bar["t"] / 1000, tz=timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ")
                    for bar in bars
                ],
                "open":   [float(bar["o"]) for bar in bars],
                "high":   [float(bar["h"]) for bar in bars],
                "low":    [float(bar["l"]) for bar in bars],
                "close":  [float(bar["c"]) for bar in bars],
                "volume": [float(bar["v"]) for bar in bars],
            }

            if timeframe == "4h":
                ohlcv = _resample_4h(ohlcv)

            log.debug(
                "Polygon: fetched %d %s bars for %s (session=%s)",
                len(ohlcv["close"]), timeframe, ticker, session,
            )
            return ohlcv

        except Exception as e:
            log.error("Polygon fetch failed for %s: %s", ticker, e)
            return None
