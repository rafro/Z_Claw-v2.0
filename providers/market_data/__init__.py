"""
Market data provider abstraction layer.
Fetch OHLCV bars from multiple sources with automatic fallback.
"""

from providers.market_data.base import MarketDataProvider
from providers.market_data.csv_provider import CSVProvider
from providers.market_data.factory import get_provider
from providers.market_data.polygon_provider import PolygonProvider
from providers.market_data.tradovate_provider import TradovateProvider
from providers.market_data.twelvedata_provider import TwelveDataProvider

__all__ = [
    "MarketDataProvider",
    "CSVProvider",
    "PolygonProvider",
    "TradovateProvider",
    "TwelveDataProvider",
    "get_provider",
]
