"""
yfinance market data provider.
Wraps the free Yahoo Finance API via the yfinance package.
Always available (yfinance is in requirements.txt) but limited to
15m+ timeframes with short history windows.
"""

from __future__ import annotations

import logging
from typing import Optional

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Symbol mapping: futures/alias -> yfinance ticker ─────────────────────────
SYMBOL_MAP = {
    "MES": "^GSPC",   "SPX500": "^GSPC",
    "MNQ": "^IXIC",   "NAS100": "^IXIC",
    "MGC": "GC=F",    "XAUUSD": "GC=F",
    "MYM": "^DJI",    "US30":   "^DJI",
    "MCL": "CL=F",    "CRUDE":  "CL=F",
    "MBT": "ZN=F",    "BONDS":  "ZN=F",
}

# ── Timeframe -> (yfinance interval, yfinance period) ────────────────────────
_TF_MAP = {
    "15m": ("15m", "5d"),    # ~130 intraday 15-min bars
    "1h":  ("1h",  "30d"),   # ~480 1h bars
    "4h":  ("1h",  "30d"),   # fetch 1h, resample -> ~120 4h bars
    "1d":  ("1d",  "3mo"),   # daily fallback
}

# ── Session definitions (UTC boundaries) ─────────────────────────────────────
SESSION_RANGES = {
    "rth":      (14, 30, 21, 0),   # 9:30-16:00 ET -> 14:30-21:00 UTC
    "ny_rth":   (14, 30, 21, 0),   # alias
    "extended": (13, 0,  22, 0),   # 8:00-17:00 ET -> 13:00-22:00 UTC
    "london":   (8,  0,  16, 30),  # 08:00-16:30 UTC
    "asia":     (0,  0,  8,  0),   # 00:00-08:00 UTC (rough)
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


def _apply_session_filter(df, session: str):
    """Filter a pandas DataFrame index by session time boundaries."""
    if session not in SESSION_RANGES:
        return df
    start_h, start_m, end_h, end_m = SESSION_RANGES[session]
    if not hasattr(df.index, "hour"):
        return df
    mask = (
        (df.index.hour > start_h)
        | ((df.index.hour == start_h) & (df.index.minute >= start_m))
    ) & (
        (df.index.hour < end_h)
        | ((df.index.hour == end_h) & (df.index.minute < end_m))
    )
    return df[mask]


class YFinanceProvider(MarketDataProvider):
    """Free market data via Yahoo Finance (yfinance package)."""

    provider_id: str = "yfinance"

    def is_available(self) -> bool:
        """yfinance is always available if the package is installed."""
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            return False

    def supported_timeframes(self) -> list[str]:
        return ["15m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return ["*"]  # accepts any yfinance-compatible ticker

    def _resolve_ticker(self, symbol: str) -> str:
        """Map futures/alias symbols to yfinance tickers."""
        return SYMBOL_MAP.get(symbol.upper(), symbol)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1d",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV via yfinance.

        Uses period-based fetching by default (matches existing behavior).
        If start/end are provided, uses date-range fetching instead.
        """
        ticker = self._resolve_ticker(symbol)
        yf_interval, yf_period = _TF_MAP.get(timeframe, ("1d", "3mo"))

        try:
            import yfinance as yf

            if start:
                df = yf.download(
                    ticker,
                    start=start,
                    end=end,
                    interval=yf_interval,
                    auto_adjust=False,
                    progress=False,
                )
            else:
                df = yf.download(
                    ticker,
                    period=yf_period,
                    interval=yf_interval,
                    auto_adjust=False,
                    progress=False,
                )

            if df.empty:
                log.warning("yfinance: no data returned for %s (%s)", symbol, ticker)
                return None

            # Flatten MultiIndex columns if present
            if hasattr(df.columns, "levels"):
                df.columns = df.columns.get_level_values(0)

            # Session filtering for intraday timeframes
            if session != "all" and timeframe in ("15m", "1h", "4h"):
                df = _apply_session_filter(df, session)
                if df.empty:
                    log.warning(
                        "yfinance: no bars after session filter '%s' for %s",
                        session, ticker,
                    )
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

            log.debug(
                "yfinance: fetched %d %s bars for %s (session=%s)",
                len(ohlcv["close"]), timeframe, ticker, session,
            )
            return ohlcv

        except ImportError:
            log.error("yfinance not installed — run: pip install yfinance pandas")
            return None
        except Exception as e:
            log.error("yfinance fetch failed for %s: %s", ticker, e)
            return None
