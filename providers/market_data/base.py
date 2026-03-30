"""
Market data provider base class.
All providers must implement fetch_ohlcv() returning a standardized dict.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class MarketDataProvider(ABC):
    """Base class for all market data providers."""

    provider_id: str = "base"

    @abstractmethod
    def is_available(self) -> bool:
        """Check if this provider is configured and reachable."""
        ...

    @abstractmethod
    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        start: Optional[str] = None,
        end: Optional[str] = None,
        session: str = "all",
    ) -> Optional[dict]:
        """
        Fetch OHLCV data.

        Args:
            symbol: Instrument symbol (e.g., "MES", "SPY", "^GSPC")
            timeframe: Bar size — "1m", "5m", "15m", "1h", "4h", "1d"
            start: Start date (ISO format or "2024-01-01")
            end: End date
            session: Trading session filter ("all", "ny_rth", "london", "asia")

        Returns:
            dict with keys: ticker, date, open, high, low, close, volume
            (all lists of equal length) or None on failure
        """
        ...

    @abstractmethod
    def supported_timeframes(self) -> list[str]:
        """Return list of supported timeframe strings."""
        ...

    @abstractmethod
    def supported_symbols(self) -> list[str]:
        """Return list of supported symbol strings (or ['*'] for all)."""
        ...

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} id={self.provider_id}>"
