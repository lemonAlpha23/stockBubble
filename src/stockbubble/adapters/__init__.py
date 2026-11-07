"""Adapters: concrete I/O implementations of the domain ports."""

from stockbubble.adapters.cache import TTLCache
from stockbubble.adapters.coingecko import CoinGeckoProvider
from stockbubble.adapters.http import JsonHttpClient
from stockbubble.adapters.logos import StockLogos
from stockbubble.adapters.openbb_quote import OpenBBProvider
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.adapters.yahoo import YahooProvider

__all__ = [
    "CoinGeckoProvider",
    "JsonHttpClient",
    "OpenBBProvider",
    "StockLogos",
    "TTLCache",
    "YahooProvider",
    "YamlUniverseRepository",
]
