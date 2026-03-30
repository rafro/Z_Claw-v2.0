"""
Tradovate market data provider — real CME futures data via Tradovate API.
Free with MyFundedFutures, Apex (Tradovate), or any Tradovate account.
Provides actual micro futures data: MES, MNQ, MGC, MYM, MCL, MBT.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Contract month codes ─────────────────────────────────────────────────────
# H=Mar, M=Jun, U=Sep, Z=Dec
_QUARTER_CODES = ["H", "M", "U", "Z"]
_QUARTER_MONTHS = [3, 6, 9, 12]

# ── Symbol mapping: alias -> root contract symbol ────────────────────────────
SYMBOL_MAP = {
    "MES": "MES",
    "MNQ": "MNQ",
    "MGC": "MGC",
    "MYM": "MYM",
    "MCL": "MCL",
    "MBT": "MBT",
    "SPX500": "MES",
    "NAS100": "MNQ",
    "US30": "MYM",
    "XAUUSD": "MGC",
    "CRUDE": "MCL",
    "BONDS": "MBT",
}

# ── Timeframe -> Tradovate elementSize (minutes) ─────────────────────────────
_TF_MAP = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


def _resolve_front_month(root_symbol: str) -> str:
    """
    Resolve a root symbol (e.g. 'MES') to the current front-month contract.

    Tries the current quarter's contract first, then falls back to the
    next quarter if the current one has likely expired.

    Returns e.g. 'MESM5' for June 2025 contract.
    """
    now = datetime.now(tz=timezone.utc)
    month = now.month
    year = now.year
    year_digit = year % 10  # last digit: 2025 -> 5

    # Find the current or next quarter
    for i, q_month in enumerate(_QUARTER_MONTHS):
        if month <= q_month:
            # Current quarter contract — but if we're in the expiry month
            # and past the third Friday (rough: day > 20), roll to next
            if month == q_month and now.day > 20:
                # Roll to next quarter
                next_i = (i + 1) % 4
                next_year_digit = year_digit if next_i != 0 else (year_digit + 1) % 10
                return f"{root_symbol}{_QUARTER_CODES[next_i]}{next_year_digit}"
            return f"{root_symbol}{_QUARTER_CODES[i]}{year_digit}"

    # Past December — roll to next year's March
    return f"{root_symbol}H{(year_digit + 1) % 10}"


class TradovateProvider(MarketDataProvider):
    """Real CME futures data via Tradovate API."""

    provider_id: str = "tradovate"

    def __init__(self) -> None:
        self._username = os.getenv("TRADOVATE_USERNAME", "")
        self._password = os.getenv("TRADOVATE_PASSWORD", "")
        self._app_id = os.getenv("TRADOVATE_APP_ID", "")
        self._cid = os.getenv("TRADOVATE_CID", "")
        self._token = os.getenv("TRADOVATE_ACCESS_TOKEN", "")
        self._expiry: Optional[str] = None

        is_demo = os.getenv("TRADOVATE_DEMO", "false").lower() == "true"
        if is_demo:
            self.base_url = "https://demo.tradovateapi.com/v1"
        else:
            self.base_url = "https://live.tradovateapi.com/v1"

    def is_available(self) -> bool:
        """Available if we have a pre-set token OR login credentials."""
        if self._token:
            return True
        return bool(self._username and self._password and self._app_id and self._cid)

    def supported_timeframes(self) -> list[str]:
        return ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]

    def supported_symbols(self) -> list[str]:
        return list(SYMBOL_MAP.keys())

    def _is_expired(self) -> bool:
        """Check if the access token has expired."""
        if not self._expiry:
            return True
        try:
            exp = datetime.fromisoformat(self._expiry.replace("Z", "+00:00"))
            return datetime.now(tz=timezone.utc) >= exp
        except (ValueError, TypeError):
            return True

    def _authenticate(self) -> None:
        """Authenticate with Tradovate and obtain an access token."""
        log.debug("tradovate: authenticating as %s", self._username)
        try:
            resp = requests.post(
                f"{self.base_url}/auth/accesstokenrequest",
                json={
                    "name": self._username,
                    "password": self._password,
                    "appId": self._app_id,
                    "appVersion": "1.0",
                    "cid": self._cid,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            self._token = data["accessToken"]
            self._expiry = data.get("expirationTime")
            log.info("tradovate: authenticated successfully (expires %s)", self._expiry)
        except requests.RequestException as e:
            log.error("tradovate: authentication failed: %s", e)
            raise RuntimeError(f"Tradovate auth failed: {e}") from e

    def _ensure_auth(self) -> None:
        """Authenticate if needed (no token or token expired)."""
        if not self._token or self._is_expired():
            self._authenticate()

    def _resolve_symbol(self, symbol: str) -> str:
        """Map a user symbol to a Tradovate contract identifier."""
        root = SYMBOL_MAP.get(symbol.upper(), symbol.upper())
        # If already a full contract spec (e.g. "MESM5"), pass through
        if len(root) > 3 and root[-1].isdigit():
            return root
        return _resolve_front_month(root)

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        """
        Make an authenticated request, retrying once on 401 (token expiry).
        """
        self._ensure_auth()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._token}"

        resp = requests.request(
            method,
            f"{self.base_url}{path}",
            headers=headers,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )

        # Retry on 401 — token may have expired between check and request
        if resp.status_code == 401:
            log.debug("tradovate: 401 received, re-authenticating")
            self._authenticate()
            headers["Authorization"] = f"Bearer {self._token}"
            resp = requests.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                timeout=30,
                **kwargs,
            )

        return resp

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV data from Tradovate.

        Returns:
            dict with keys: ticker, date, open, high, low, close, volume
            or None on failure.
        """
        contract = self._resolve_symbol(symbol)
        element_size = _TF_MAP.get(timeframe, 1)

        chart_desc = {
            "underlyingType": "MinuteBar",
            "elementSize": element_size,
            "elementSizeUnit": "UnderlyingUnits",
        }

        params: dict = {
            "symbol": contract,
            "chartDescription": json.dumps(chart_desc),
        }

        # Add date range if specified
        if start:
            params["startTimestamp"] = start
        if end:
            params["endTimestamp"] = end

        try:
            resp = self._request("GET", "/md/getchart", params=params)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error("tradovate: fetch failed for %s: %s", contract, e)
            return None
        except ValueError as e:
            log.error("tradovate: invalid JSON response for %s: %s", contract, e)
            return None

        # Parse bars from response
        bars = data.get("bars", data.get("d", []))
        if not bars:
            log.warning("tradovate: no bars returned for %s (%s)", symbol, contract)
            return None

        dates: list[str] = []
        opens: list[float] = []
        highs: list[float] = []
        lows: list[float] = []
        closes: list[float] = []
        volumes: list[float] = []

        for bar in bars:
            # Tradovate bar format may vary — handle both dict and list
            if isinstance(bar, dict):
                ts = bar.get("timestamp", bar.get("t", ""))
                o = float(bar.get("open", bar.get("o", 0)))
                h = float(bar.get("high", bar.get("h", 0)))
                l = float(bar.get("low", bar.get("l", 0)))
                c = float(bar.get("close", bar.get("c", 0)))
                v = float(bar.get("volume", bar.get("v", 0)))
            elif isinstance(bar, list) and len(bar) >= 6:
                ts, o, h, l, c, v = bar[0], float(bar[1]), float(bar[2]), float(bar[3]), float(bar[4]), float(bar[5])
            else:
                continue

            dates.append(str(ts))
            opens.append(o)
            highs.append(h)
            lows.append(l)
            closes.append(c)
            volumes.append(v)

        if not dates:
            log.warning("tradovate: no valid bars parsed for %s", contract)
            return None

        log.debug(
            "tradovate: fetched %d %s bars for %s (%s)",
            len(dates), timeframe, symbol, contract,
        )
        return {
            "ticker": contract,
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
