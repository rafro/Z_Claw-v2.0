"""
CSV market data provider — reads exported OHLCV CSV files.
Supports TradingView, NinjaTrader, and generic CSV formats.
Place files in state/trading/historical/{symbol}_{timeframe}.csv
No API key needed — just export and drop files in the directory.
"""

from __future__ import annotations

import csv
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)

# ── Default CSV directory (relative to project root) ─────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_CSV_DIR = _ROOT / "state" / "trading" / "historical"

# ── Futures -> ETF proxy fallback (try ETF file if futures file missing) ─────
FUTURES_TO_ETF = {
    "MES": "SPY",   "SPX500": "SPY",
    "MNQ": "QQQ",   "NAS100": "QQQ",
    "MGC": "GLD",   "XAUUSD": "GLD",
    "MYM": "DIA",   "US30":   "DIA",
    "MCL": "USO",   "CRUDE":  "USO",
    "MBT": "TLT",   "BONDS":  "TLT",
}

# ── Known timeframe tokens ───────────────────────────────────────────────────
_VALID_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h", "4h", "1d", "1w"}


def _parse_date(raw: str) -> Optional[datetime]:
    """
    Parse a date/datetime string flexibly.

    Supports:
        - ISO 8601:       2024-06-15T14:30:00, 2024-06-15 14:30:00
        - US format:      06/15/2024, 06/15/2024 14:30
        - Date only:      2024-06-15, 20240615
        - Unix timestamp: 1718457000 (int or float)
    """
    raw = raw.strip().strip('"').strip("'")
    if not raw:
        return None

    # Unix timestamp (integer or float, 10+ digits)
    if re.match(r"^\d{10,}(\.\d+)?$", raw):
        return datetime.fromtimestamp(float(raw), tz=timezone.utc)

    # Try common formats in order
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
        "%Y%m%d %H:%M:%S",
        "%Y%m%d",
    ):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue

    log.warning("csv_provider: could not parse date '%s'", raw)
    return None


def _detect_format(header_line: str) -> tuple[str, dict[str, int]]:
    """
    Detect CSV format from header line.

    Returns:
        (delimiter, column_map) where column_map maps
        canonical names (date, open, high, low, close, volume) to column indices.
    """
    # Detect delimiter
    if ";" in header_line:
        delimiter = ";"
    elif "\t" in header_line:
        delimiter = "\t"
    else:
        delimiter = ","

    cols = [c.strip().strip('"').strip("'").lower() for c in header_line.split(delimiter)]

    col_map: dict[str, int] = {}

    # Map date/time column(s)
    for i, c in enumerate(cols):
        if c in ("time", "date", "datetime", "timestamp", "date/time"):
            col_map["date"] = i
            break

    # NinjaTrader has separate Date and Time columns
    if "date" not in col_map:
        for i, c in enumerate(cols):
            if c == "date":
                col_map["date"] = i
                break

    # Check for separate time column (NinjaTrader style)
    for i, c in enumerate(cols):
        if c == "time" and "date" in col_map and col_map["date"] != i:
            col_map["time"] = i
            break

    # Map OHLCV columns (case-insensitive)
    for target in ("open", "high", "low", "close", "volume"):
        for i, c in enumerate(cols):
            if c == target or c == target[0]:  # some CSVs use 'o','h','l','c','v'
                col_map[target] = i
                break

    return delimiter, col_map


class CSVProvider(MarketDataProvider):
    """Market data from CSV file exports (TradingView, NinjaTrader, generic)."""

    provider_id: str = "csv"

    def __init__(self) -> None:
        csv_dir_env = os.getenv("MARKET_DATA_CSV_DIR", "")
        if csv_dir_env:
            self._csv_dir = Path(csv_dir_env)
            if not self._csv_dir.is_absolute():
                self._csv_dir = _ROOT / self._csv_dir
        else:
            self._csv_dir = _DEFAULT_CSV_DIR

    def is_available(self) -> bool:
        """Available if the CSV directory exists and contains at least one .csv file."""
        if not self._csv_dir.is_dir():
            return False
        return any(self._csv_dir.glob("*.csv"))

    def supported_timeframes(self) -> list[str]:
        """Scan filenames for timeframe suffixes."""
        timeframes: set[str] = set()
        if not self._csv_dir.is_dir():
            return []
        for f in self._csv_dir.glob("*.csv"):
            parts = f.stem.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in _VALID_TIMEFRAMES:
                timeframes.add(parts[1])
        return sorted(timeframes) if timeframes else ["unknown"]

    def supported_symbols(self) -> list[str]:
        """Scan filenames for symbol prefixes."""
        symbols: set[str] = set()
        if not self._csv_dir.is_dir():
            return []
        for f in self._csv_dir.glob("*.csv"):
            parts = f.stem.rsplit("_", 1)
            if len(parts) == 2 and parts[1] in _VALID_TIMEFRAMES:
                symbols.add(parts[0].upper())
            else:
                # No timeframe suffix — whole stem is the symbol
                symbols.add(f.stem.upper())
        return sorted(symbols)

    def _find_csv(self, symbol: str, timeframe: str) -> Optional[Path]:
        """
        Locate CSV file for a symbol/timeframe combo.

        Search order:
            1. {symbol}_{timeframe}.csv  (exact match)
            2. {symbol}.csv              (no timeframe suffix — assumes correct)
            3. {etf_proxy}_{timeframe}.csv  (ETF fallback)
            4. {etf_proxy}.csv
        """
        sym = symbol.upper()
        candidates = [
            self._csv_dir / f"{sym}_{timeframe}.csv",
            self._csv_dir / f"{sym}.csv",
        ]

        # ETF proxy fallback
        etf = FUTURES_TO_ETF.get(sym)
        if etf:
            candidates.append(self._csv_dir / f"{etf}_{timeframe}.csv")
            candidates.append(self._csv_dir / f"{etf}.csv")

        # Case-insensitive search: also try lowercase filenames
        for candidate in list(candidates):
            lower = candidate.parent / candidate.name.lower()
            if lower != candidate:
                candidates.append(lower)

        for path in candidates:
            if path.is_file():
                log.debug("csv_provider: found %s", path)
                return path

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
        Load OHLCV data from a CSV file.

        Returns:
            dict with keys: ticker, date, open, high, low, close, volume
            or None if no matching CSV found.
        """
        csv_path = self._find_csv(symbol, timeframe)
        if csv_path is None:
            log.debug(
                "csv_provider: no CSV found for %s/%s in %s",
                symbol, timeframe, self._csv_dir,
            )
            return None

        # Parse start/end filters
        start_dt = _parse_date(start) if start else None
        end_dt = _parse_date(end) if end else None

        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                header_line = f.readline()
                delimiter, col_map = _detect_format(header_line)

                if "date" not in col_map:
                    log.warning("csv_provider: no date column found in %s", csv_path)
                    return None

                dates: list[str] = []
                opens: list[float] = []
                highs: list[float] = []
                lows: list[float] = []
                closes: list[float] = []
                volumes: list[float] = []

                reader = csv.reader(f, delimiter=delimiter)
                for row in reader:
                    if not row or not row[0].strip():
                        continue

                    # Parse date (combine Date + Time for NinjaTrader)
                    date_str = row[col_map["date"]].strip()
                    if "time" in col_map:
                        date_str = f"{date_str} {row[col_map['time']].strip()}"

                    dt = _parse_date(date_str)
                    if dt is None:
                        continue

                    # Apply date range filters
                    if start_dt and dt < start_dt:
                        continue
                    if end_dt and dt > end_dt:
                        continue

                    try:
                        o = float(row[col_map.get("open", 1)])
                        h = float(row[col_map.get("high", 2)])
                        l = float(row[col_map.get("low", 3)])
                        c = float(row[col_map.get("close", 4)])
                        v = float(row[col_map.get("volume", 5)]) if "volume" in col_map else 0.0
                    except (ValueError, IndexError):
                        continue

                    dates.append(str(dt))
                    opens.append(o)
                    highs.append(h)
                    lows.append(l)
                    closes.append(c)
                    volumes.append(v)

            if not dates:
                log.warning("csv_provider: no valid rows in %s", csv_path)
                return None

            log.debug(
                "csv_provider: loaded %d bars from %s (symbol=%s, tf=%s)",
                len(dates), csv_path.name, symbol, timeframe,
            )
            return {
                "ticker": symbol.upper(),
                "date": dates,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }

        except Exception as e:
            log.error("csv_provider: failed to read %s: %s", csv_path, e)
            return None
