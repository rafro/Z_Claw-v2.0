"""
Market data provider abstraction layer.
Fetch OHLCV bars from multiple sources with automatic fallback.
"""

from providers.market_data.base import MarketDataProvider
from providers.market_data.factory import get_provider

__all__ = ["MarketDataProvider", "get_provider"]
