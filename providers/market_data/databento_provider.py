"""
Databento market data provider.
Paid CME micro futures data — highest quality, actual exchange data.
Uses the `databento` Python package if installed, falls back to REST API.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Symbol mapping: alias -> Databento instrument ────────────────────────────
SYMBOL_MAP = {
    "MES": "MES.FUT.CME",
    "MNQ": "MNQ.FUT.CME",
    "MGC": "MGC.FUT.CME",
    "MYM": "MYM.FUT.CME",
    "MCL": "MCL.FUT.CME",
    "MBT": "MBT.FUT.CME",
}

# ── Timeframe -> Databento schema / granularity ──────────────────────────────
_TF_MAP = {
    "1m":  "ohlcv-1m",
    "5m":  "ohlcv-5m",  # custom resample from 1m if not natively supported
    "15m": "ohlcv-15m",
    "1h":  "ohlcv-1h",
    "4h":  "ohlcv-1h",  # fetch 1h, resample to 4h
    "1d":  "ohlcv-1d",
}

REST_URL = "https://hist.databento.com/v0/timeseries.get_range"

# ── Session definitions (UTC boundaries) ─────────────────────────────────────
SESSION_RANGES = {
    "rth":      (14, 30, 21, 0),
    "ny_rth":   (14, 30, 21, 0),
    "extended": (13, 0,  22, 0),
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


def _filter_session_timestamps(
    dates: list[str], session: str,
) -> list[int]:
    """Return indices of timestamps that fall within the session window."""
    if session not in SESSION_RANGES:
        return list(range(len(dates)))
    start_h, start_m, end_h, end_m = SESSION_RANGES[session]
    indices = []
    for idx, ts in enumerate(dates):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            # Fallback: try parsing as space-separated datetime
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                indices.append(idx)  # keep on parse failure
                continue
        h, m = dt.hour, dt.minute
        after_start = (h > start_h) or (h == start_h and m >= start_m)
        before_end = (h < end_h) or (h == end_h and m < end_m)
        if after_start and before_end:
            indices.append(idx)
    return indices


class DatabentoProvider(MarketDataProvider):
    """Paid CME micro futures data via Databento."""

    provider_id: str = "databento"

    def __init__(self) -> None:
        self._api_key = os.getenv("DATABENTO_API_KEY", "")
        self._client = None  # lazy-loaded databento.Historical client

    def is_available(self) -> bool:
        """Check API key is set. Optionally verifies connectivity."""
        if not self._api_key:
            return False
        # Try the Python package first, then fall back to REST check
        try:
            import databento  # noqa: F401
            return True
        except ImportError:
            pass
        # Verify REST API is reachable
        try:
            import requests
            resp = requests.get(
                REST_URL,
                params={"key": self._api_key},
                timeout=10,
            )
            # 400 = bad params but key valid; 401/403 = bad key
            return resp.status_code not in (401, 403)
        except Exception:
            return False

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return list(SYMBOL_MAP.keys())

    def _resolve_symbol(self, symbol: str) -> str:
        """Map alias to Databento instrument ID."""
        return SYMBOL_MAP.get(symbol.upper(), symbol.upper())

    def _get_client(self):
        """Lazy-load the databento Historical client."""
        if self._client is None:
            try:
                import databento as db
                self._client = db.Historical(key=self._api_key)
            except ImportError:
                self._client = None
        return self._client

    def _fetch_via_package(
        self,
        db_symbol: str,
        schema: str,
        start: str,
        end: Optional[str],
    ) -> Optional[dict]:
        """Fetch using the databento Python package."""
        client = self._get_client()
        if client is None:
            return None

        try:
            import databento as db

            params = {
                "dataset": "GLBX.MDP3",
                "symbols": [db_symbol],
                "schema": schema,
                "start": start,
            }
            if end:
                params["end"] = end

            data = client.timeseries.get_range(**params)
            df = data.to_df()

            if df.empty:
                return None

            return {
                "ticker": db_symbol,
                "date":   [str(d) for d in df.index],
                "open":   [float(v) for v in df["open"].tolist()],
                "high":   [float(v) for v in df["high"].tolist()],
                "low":    [float(v) for v in df["low"].tolist()],
                "close":  [float(v) for v in df["close"].tolist()],
                "volume": [float(v) for v in df["volume"].tolist()],
            }
        except Exception as e:
            log.error("Databento package fetch failed for %s: %s", db_symbol, e)
            return None

    def _fetch_via_rest(
        self,
        db_symbol: str,
        schema: str,
        start: str,
        end: Optional[str],
    ) -> Optional[dict]:
        """Fetch using the Databento REST API (fallback when package not installed)."""
        try:
            import requests

            params = {
                "key": self._api_key,
                "dataset": "GLBX.MDP3",
                "symbols": db_symbol,
                "schema": schema,
                "start": start,
                "encoding": "json",
                "stype_in": "parent",
            }
            if end:
                params["end"] = end

            resp = requests.get(REST_URL, params=params, timeout=60)

            if resp.status_code != 200:
                log.error(
                    "Databento REST API error %d for %s: %s",
                    resp.status_code, db_symbol, resp.text[:200],
                )
                return None

            records = resp.json()
            if not records:
                return None

            return {
                "ticker": db_symbol,
                "date":   [r.get("ts_event", r.get("hd", {}).get("ts_event", "")) for r in records],
                "open":   [float(r["open"]) / 1e9 for r in records],  # Databento fixed-point
                "high":   [float(r["high"]) / 1e9 for r in records],
                "low":    [float(r["low"]) / 1e9 for r in records],
                "close":  [float(r["close"]) / 1e9 for r in records],
                "volume": [float(r.get("volume", 0)) for r in records],
            }
        except ImportError:
            log.error("requests package not installed")
            return None
        except Exception as e:
            log.error("Databento REST fetch failed for %s: %s", db_symbol, e)
            return None

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV via Databento.

        Tries the databento Python package first, falls back to REST API.
        """
        if not self._api_key:
            log.error("Databento API key not configured")
            return None

        db_symbol = self._resolve_symbol(symbol)
        schema = _TF_MAP.get(timeframe)
        if not schema:
            log.error("Databento: unsupported timeframe '%s'", timeframe)
            return None

        # For 4h, fetch 1h and resample
        fetch_schema = "ohlcv-1h" if timeframe == "4h" else schema

        # Default start to 60 days ago if not specified
        if not start:
            from datetime import timedelta
            start = (
                datetime.now(timezone.utc) - timedelta(days=60)
            ).strftime("%Y-%m-%d")

        try:
            # Try Python package first, then REST
            ohlcv = self._fetch_via_package(db_symbol, fetch_schema, start, end)
            if ohlcv is None:
                ohlcv = self._fetch_via_rest(db_symbol, fetch_schema, start, end)

            if ohlcv is None:
                log.warning("Databento: no data returned for %s (%s)", symbol, db_symbol)
                return None

            # Session filtering for intraday timeframes
            if session != "all" and timeframe in ("1m", "5m", "15m", "1h", "4h"):
                keep = _filter_session_timestamps(ohlcv["date"], session)
                if not keep:
                    log.warning(
                        "Databento: no bars after session filter '%s' for %s",
                        session, db_symbol,
                    )
                    return None
                ohlcv = {
                    "ticker": ohlcv["ticker"],
                    "date":   [ohlcv["date"][i] for i in keep],
                    "open":   [ohlcv["open"][i] for i in keep],
                    "high":   [ohlcv["high"][i] for i in keep],
                    "low":    [ohlcv["low"][i] for i in keep],
                    "close":  [ohlcv["close"][i] for i in keep],
                    "volume": [ohlcv["volume"][i] for i in keep],
                }

            if timeframe == "4h":
                ohlcv = _resample_4h(ohlcv)

            log.debug(
                "Databento: fetched %d %s bars for %s (session=%s)",
                len(ohlcv["close"]), timeframe, db_symbol, session,
            )
            return ohlcv

        except Exception as e:
            log.error("Databento fetch failed for %s: %s", db_symbol, e)
            return None
