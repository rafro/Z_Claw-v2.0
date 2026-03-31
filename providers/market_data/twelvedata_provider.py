"""
Twelve Data market data provider — 3+ years of 1-minute ETF bars on free tier.
Works internationally (Canada, EU, etc.). 800 API credits/day on free plan.
Best free option for daytrading backtesting with deep 1-minute history.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

BASE_URL = "https://api.twelvedata.com"
MAX_OUTPUTSIZE = 5000  # max bars per request

# ── Futures/alias -> ETF proxy ────────────────────────────────────────────────
SYMBOL_MAP = {
    "MES": "SPY",   "SPX500": "SPY",
    "MNQ": "QQQ",   "NAS100": "QQQ",
    "MGC": "GLD",   "XAUUSD": "GLD",
    "MYM": "DIA",   "US30":   "DIA",
    "MCL": "USO",   "CRUDE":  "USO",
    "MBT": "TLT",   "BONDS":  "TLT",
}

# ── Timeframe mapping ─────────────────────────────────────────────────────────
_TF_MAP = {
    "1m":  "1min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1h",
    "4h":  "4h",
    "1d":  "1day",
}

# ── Session definitions (UTC boundaries) ──────────────────────────────────────
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


def _filter_session_by_datetime(dates: list[str], session: str) -> list[int]:
    """Return indices of bars that fall within the given session (UTC)."""
    if session not in SESSION_RANGES:
        return list(range(len(dates)))
    start_h, start_m, end_h, end_m = SESSION_RANGES[session]
    indices = []
    for i, dt_str in enumerate(dates):
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        h, m = dt.hour, dt.minute
        after_start = (h > start_h) or (h == start_h and m >= start_m)
        before_end = (h < end_h) or (h == end_h and m < end_m)
        if after_start and before_end:
            indices.append(i)
    return indices


class TwelveDataProvider(MarketDataProvider):
    """Twelve Data — 3+ years of 1-minute ETF bars. Works internationally."""

    provider_id: str = "twelvedata"

    def __init__(self) -> None:
        self._api_key = os.getenv("TWELVEDATA_API_KEY", "")

    def is_available(self) -> bool:
        """Check API key is set and a test request succeeds."""
        if not self._api_key:
            return False
        try:
            resp = requests.get(
                f"{BASE_URL}/time_series",
                params={
                    "symbol": "SPY",
                    "interval": "1day",
                    "outputsize": "1",
                    "apikey": self._api_key,
                },
                timeout=10,
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return data.get("status") == "ok"
        except Exception:
            return False

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return ["*"]  # any stock/ETF ticker

    def _resolve_symbol(self, symbol: str) -> str:
        """Map futures/alias to ETF proxy ticker."""
        upper = symbol.upper()
        return SYMBOL_MAP.get(upper, upper)

    def _fetch_page(
        self,
        ticker: str,
        interval: str,
        start_date: str,
        end_date: str,
    ) -> Optional[list[dict]]:
        """
        Fetch up to 5000 bars from Twelve Data.

        Returns list of bar dicts (in API order, newest first),
        None on rate limit / credit exhaustion,
        empty list on other errors.
        """
        params = {
            "symbol": ticker,
            "interval": interval,
            "start_date": start_date,
            "end_date": end_date,
            "outputsize": str(MAX_OUTPUTSIZE),
            "format": "JSON",
            "timezone": "America/New_York",
            "apikey": self._api_key,
        }

        resp = requests.get(f"{BASE_URL}/time_series", params=params, timeout=30)

        if resp.status_code != 200:
            log.error(
                "Twelve Data API HTTP %d for %s: %s",
                resp.status_code, ticker, resp.text[:200],
            )
            return []

        data = resp.json()

        # Check for error response
        if data.get("status") == "error":
            msg = data.get("message", "unknown error")
            if "credits" in msg.lower() or "rate" in msg.lower() or "limit" in msg.lower():
                log.warning(
                    "Twelve Data: rate/credit limit hit for %s — %s",
                    ticker, msg,
                )
                return None
            log.error("Twelve Data API error for %s: %s", ticker, msg)
            return []

        values = data.get("values", [])
        return values

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV via Twelve Data API.

        Handles pagination (multiple requests with adjusted date ranges)
        for periods exceeding 5000 bars. Rate/credit limit returns None
        to let the fallback chain handle it.
        """
        if not self._api_key:
            log.error("Twelve Data API key not configured")
            return None

        interval = _TF_MAP.get(timeframe)
        if not interval:
            log.error("Twelve Data: unsupported timeframe '%s'", timeframe)
            return None

        # For 4h, fetch 1h and resample
        fetch_interval = interval
        if timeframe == "4h":
            fetch_interval = "1h"

        ticker = self._resolve_symbol(symbol)

        # Default date range
        now = datetime.now(timezone.utc)
        if not end:
            end = now.strftime("%Y-%m-%d")
        if not start:
            if timeframe == "1d":
                start = (now - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
            else:
                start = (now - timedelta(days=2 * 365)).strftime("%Y-%m-%d")

        try:
            # Pagination: collect all bars across multiple requests
            all_values: list[dict] = []
            current_end = end

            while True:
                page = self._fetch_page(ticker, fetch_interval, start, current_end)

                # Rate/credit limit — bubble up None for fallback chain
                if page is None:
                    return None

                if not page:
                    break

                all_values.extend(page)

                # If we got fewer than max, we have all the data
                if len(page) < MAX_OUTPUTSIZE:
                    break

                # Data comes newest-first; last entry is the oldest in this page.
                # Set next request's end_date to just before the oldest bar.
                oldest_dt_str = page[-1]["datetime"]
                # Parse "2026-03-30 15:59:00" format
                oldest_dt = datetime.strptime(oldest_dt_str, "%Y-%m-%d %H:%M:%S")
                # Move back 1 minute to avoid overlap
                current_end = (oldest_dt - timedelta(minutes=1)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )

                # Safety: if we've pushed past the start date, stop
                if current_end < start:
                    break

            if not all_values:
                log.warning("Twelve Data: no data returned for %s (%s)", symbol, ticker)
                return None

            # Reverse: Twelve Data returns newest first, we need oldest first
            all_values.reverse()

            # Deduplicate by datetime (overlapping pagination boundaries)
            seen = set()
            unique_values = []
            for v in all_values:
                dt_key = v["datetime"]
                if dt_key not in seen:
                    seen.add(dt_key)
                    unique_values.append(v)

            # Convert string values to float and build standard OHLCV dict
            # Twelve Data datetime is "YYYY-MM-DD HH:MM:SS" in America/New_York.
            # Convert to ISO 8601 UTC-ish format for consistency with other providers.
            ohlcv = {
                "ticker": ticker,
                "date":   [v["datetime"].replace(" ", "T") + "Z" for v in unique_values],
                "open":   [float(v["open"]) for v in unique_values],
                "high":   [float(v["high"]) for v in unique_values],
                "low":    [float(v["low"]) for v in unique_values],
                "close":  [float(v["close"]) for v in unique_values],
                "volume": [float(v["volume"]) for v in unique_values],
            }

            # Apply session filter before resampling
            if session != "all" and timeframe in ("1m", "5m", "15m", "30m", "1h", "4h"):
                indices = _filter_session_by_datetime(ohlcv["date"], session)
                if not indices:
                    log.warning(
                        "Twelve Data: no bars after session filter '%s' for %s",
                        session, ticker,
                    )
                    return None
                ohlcv = {
                    "ticker": ticker,
                    "date":   [ohlcv["date"][i] for i in indices],
                    "open":   [ohlcv["open"][i] for i in indices],
                    "high":   [ohlcv["high"][i] for i in indices],
                    "low":    [ohlcv["low"][i] for i in indices],
                    "close":  [ohlcv["close"][i] for i in indices],
                    "volume": [ohlcv["volume"][i] for i in indices],
                }

            if timeframe == "4h":
                ohlcv = _resample_4h(ohlcv)

            log.debug(
                "Twelve Data: fetched %d %s bars for %s (session=%s)",
                len(ohlcv["close"]), timeframe, ticker, session,
            )
            return ohlcv

        except Exception as e:
            log.error("Twelve Data fetch failed for %s: %s", ticker, e)
            return None
