"""History use case: sparkline data for the detail panel."""

from __future__ import annotations

from dataclasses import dataclass

from stockbubble.application.feed import MarketFeed
from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.errors import HistoryUnavailable, SymbolNotFound
from stockbubble.domain.models import PricePoint, Timeframe
from stockbubble.domain.ports import PriceSeriesProvider, QuoteCache, UniverseRepository


@dataclass(frozen=True, slots=True)
class SeriesResult:
    symbol: str
    timeframe: Timeframe
    points: tuple[PricePoint, ...]
    source: str


class HistoryService:
    """Read the series cache before resolving a symbol through the quote feed."""

    def __init__(
        self,
        *,
        universes: UniverseRepository,
        feed: MarketFeed,
        providers: ProviderRegistry,
        cache: QuoteCache,
    ) -> None:
        self._universes = universes
        self._feed = feed
        self._providers = providers
        self._cache = cache

    async def series(
        self,
        *,
        universe_id: str,
        symbol: str,
        timeframe: Timeframe,
    ) -> SeriesResult:
        universe = self._universes.get(universe_id)
        wanted = symbol.strip().upper()
        provider = self._providers.get(universe.provider)
        if not isinstance(provider, PriceSeriesProvider):
            raise HistoryUnavailable(f"provider {provider.provider_id!r} has no history support")

        key = (
            f"series:{provider.provider_id}:{universe.id}:{universe.currency}:"
            f"{wanted}:{timeframe.value}"
        )

        async def load_series() -> SeriesResult:
            feed = await self._feed.quotes(universe)
            quote = next((item for item in feed.quotes if item.symbol == wanted), None)
            if quote is None:
                raise SymbolNotFound(f"symbol {wanted!r} is not part of universe {universe.id!r}")
            points = await provider.fetch_series(
                external_id=quote.external_id or quote.symbol,
                timeframe=timeframe,
                currency=quote.currency,
            )
            if not points:
                raise HistoryUnavailable(f"no history for {wanted} ({timeframe.value})")
            return SeriesResult(
                symbol=wanted,
                timeframe=timeframe,
                points=tuple(points),
                source=provider.provider_id,
            )

        cached = await self._cache.load(key, load_series)
        return cached.value
