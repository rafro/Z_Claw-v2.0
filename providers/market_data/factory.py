"""
Market data provider factory.
Selects the best available provider based on configuration and API keys.
Priority: tradovate > databento > alpaca > csv > yfinance
"""

from __future__ import annotations

import logging
import os

from providers.market_data.base import MarketDataProvider

log = logging.getLogger(__name__)


def get_provider(preference: str = "auto") -> MarketDataProvider:
    """
    Get a market data provider.

    Args:
        preference: Provider selection strategy.
            "auto"       — best available (tradovate > databento > alpaca > csv > yfinance)
            "yfinance"   — force Yahoo Finance
            "alpaca"     — force Alpaca Markets
            "databento"  — force Databento
            "tradovate"  — force Tradovate
            "csv"        — force CSV file provider

    Returns:
        A configured MarketDataProvider instance.

    Raises:
        RuntimeError: If the requested provider is not available and no
                      fallback is possible (only for explicit preference).
    """
    # Resolve "auto" from env var if set
    if preference == "auto":
        preference = os.getenv("MARKET_DATA_PROVIDER", "auto")

    # ── Explicit provider requests ───────────────────────────────────────
    if preference == "tradovate":
        from providers.market_data.tradovate_provider import TradovateProvider
        provider = TradovateProvider()
        if provider.is_available():
            log.info("Market data provider: tradovate (explicit)")
            return provider
        raise RuntimeError(
            "Tradovate provider requested but not available — "
            "check TRADOVATE_USERNAME/PASSWORD/APP_ID/CID or "
            "TRADOVATE_ACCESS_TOKEN in .env"
        )

    if preference == "databento":
        from providers.market_data.databento_provider import DatabentoProvider
        provider = DatabentoProvider()
        if provider.is_available():
            log.info("Market data provider: databento (explicit)")
            return provider
        raise RuntimeError(
            "Databento provider requested but not available — "
            "check DATABENTO_API_KEY in .env"
        )

    if preference == "alpaca":
        from providers.market_data.alpaca_provider import AlpacaProvider
        provider = AlpacaProvider()
        if provider.is_available():
            log.info("Market data provider: alpaca (explicit)")
            return provider
        raise RuntimeError(
            "Alpaca provider requested but not available — "
            "check ALPACA_API_KEY and ALPACA_SECRET_KEY in .env"
        )

    if preference == "csv":
        from providers.market_data.csv_provider import CSVProvider
        provider = CSVProvider()
        if provider.is_available():
            log.info("Market data provider: csv (explicit)")
            return provider
        raise RuntimeError(
            "CSV provider requested but not available — "
            "no .csv files found in state/trading/historical/ "
            "(or MARKET_DATA_CSV_DIR)"
        )

    if preference == "yfinance":
        from providers.market_data.yfinance_provider import YFinanceProvider
        provider = YFinanceProvider()
        if provider.is_available():
            log.info("Market data provider: yfinance (explicit)")
            return provider
        raise RuntimeError(
            "yfinance provider requested but not available — "
            "run: pip install yfinance pandas"
        )

    # ── Auto selection: best available ───────────────────────────────────
    # 1. Tradovate (free with prop firm account — actual CME futures data)
    if os.getenv("TRADOVATE_ACCESS_TOKEN") or (
        os.getenv("TRADOVATE_USERNAME") and os.getenv("TRADOVATE_PASSWORD")
    ):
        try:
            from providers.market_data.tradovate_provider import TradovateProvider
            provider = TradovateProvider()
            if provider.is_available():
                log.info("Market data provider: tradovate (auto)")
                return provider
        except Exception as e:
            log.debug("Tradovate auto-check failed: %s", e)

    # 2. Databento (paid, highest quality CME futures data)
    if os.getenv("DATABENTO_API_KEY"):
        try:
            from providers.market_data.databento_provider import DatabentoProvider
            provider = DatabentoProvider()
            if provider.is_available():
                log.info("Market data provider: databento (auto)")
                return provider
        except Exception as e:
            log.debug("Databento auto-check failed: %s", e)

    # 3. Alpaca (free, good quality stock/ETF data with 1m history)
    if os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY"):
        try:
            from providers.market_data.alpaca_provider import AlpacaProvider
            provider = AlpacaProvider()
            if provider.is_available():
                log.info("Market data provider: alpaca (auto)")
                return provider
        except Exception as e:
            log.debug("Alpaca auto-check failed: %s", e)

    # 4. CSV (local file exports — better quality than yfinance proxies)
    try:
        from providers.market_data.csv_provider import CSVProvider
        provider = CSVProvider()
        if provider.is_available():
            log.info("Market data provider: csv (auto)")
            return provider
    except Exception as e:
        log.debug("CSV auto-check failed: %s", e)

    # 5. yfinance (always available, no API key needed)
    from providers.market_data.yfinance_provider import YFinanceProvider
    provider = YFinanceProvider()
    log.info("Market data provider: yfinance (fallback)")
    return provider
