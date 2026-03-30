"""
Market data provider abstraction layer.
Fetch OHLCV bars from multiple sources with automatic fallback.
"""

from providers.market_data.base import MarketDataProvider
from providers.market_data.csv_provider import CSVProvider
from providers.market_data.factory import get_provider
from providers.market_data.tradovate_provider import TradovateProvider

__all__ = [
    "MarketDataProvider",
    "CSVProvider",
    "TradovateProvider",
    "get_provider",
]
