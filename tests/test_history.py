from __future__ import annotations

import asyncio

import pytest
from tests.conftest import FakeProvider, make_quote

from stockbubble.adapters.cache import TTLCache
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.application.feed import MarketFeed
from stockbubble.application.history import HistoryService
from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.errors import HistoryUnavailable, SymbolNotFound, UniverseNotFound
from stockbubble.domain.models import PricePoint, ProviderResult, Timeframe, Universe


def build_service(
    universe: Universe,
    provider: FakeProvider,
    *,
    cache: TTLCache | None = None,
    quote_cache: TTLCache | None = None,
) -> HistoryService:
    registry = ProviderRegistry()
    registry.register(provider)
    return HistoryService(
        universes=YamlUniverseRepository([universe]),
        feed=MarketFeed(providers=registry, cache=quote_cache or TTLCache(60.0)),
        providers=registry,
        cache=cache or TTLCache(60.0),
    )


async def test_series_uses_cached_quote_identity(universe: Universe) -> None:
    points = (PricePoint(t=1, c=2.0), PricePoint(t=2, c=3.0))
    provider = FakeProvider(
        quotes=(make_quote("AAA", external_id="ext-aaa"),),
        points=points,
    )
    service = build_service(universe, provider)

    result = await service.series(universe_id="equity", symbol="aaa", timeframe=Timeframe.M1)

    assert result.symbol == "AAA"
    assert result.timeframe is Timeframe.M1
    assert result.source == "fake"
    assert result.points == points
    assert provider.series_calls == 1
    assert provider.quote_calls == 1


async def test_series_is_cached_per_timeframe(universe: Universe) -> None:
    provider = FakeProvider(
        quotes=(make_quote("AAA"),),
        points=(PricePoint(t=1, c=2.0),),
    )
    service = build_service(universe, provider)

    await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)
    await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)
    assert provider.series_calls == 1

    await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.Y1)
    assert provider.series_calls == 2


async def test_cached_history_does_not_refresh_expired_quotes(universe: Universe) -> None:
    now = [100.0]
    provider = FakeProvider(quotes=(make_quote("AAA"),), points=(PricePoint(t=1, c=2.0),))
    service = build_service(
        universe,
        provider,
        quote_cache=TTLCache(1.0, clock=lambda: now[0]),
        cache=TTLCache(300.0, clock=lambda: now[0]),
    )
    first = await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)
    now[0] += 60
    provider.error = RuntimeError("quote source unavailable")
    second = await service.series(universe_id="equity", symbol="aaa", timeframe=Timeframe.M1)
    assert second == first
    assert provider.quote_calls == provider.series_calls == 1


async def test_concurrent_history_shares_complete_load(universe: Universe) -> None:
    provider = FakeProvider(quotes=(make_quote("AAA"),), points=(PricePoint(t=1, c=2.0),))
    service = build_service(universe, provider)
    results = await asyncio.gather(
        *[
            service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)
            for _ in range(5)
        ]
    )
    assert all(result == results[0] for result in results)
    assert provider.quote_calls == provider.series_calls == 1


async def test_unknown_symbol_raises(universe: Universe) -> None:
    service = build_service(universe, FakeProvider(quotes=(make_quote("AAA"),)))
    with pytest.raises(SymbolNotFound):
        await service.series(universe_id="equity", symbol="ZZZ", timeframe=Timeframe.M1)


async def test_unknown_universe_raises(universe: Universe) -> None:
    service = build_service(universe, FakeProvider())
    with pytest.raises(UniverseNotFound):
        await service.series(universe_id="other", symbol="AAA", timeframe=Timeframe.M1)


async def test_empty_series_raises_history_unavailable(universe: Universe) -> None:
    provider = FakeProvider(quotes=(make_quote("AAA"),), points=())
    service = build_service(universe, provider)
    with pytest.raises(HistoryUnavailable):
        await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)


async def test_provider_without_series_support_raises(universe: Universe) -> None:
    class QuotesOnly:
        """Satisfies MarketDataProvider but deliberately not PriceSeriesProvider."""

        provider_id = "fake"
        asset_class = universe.asset_class
        supported_windows = frozenset({universe.default_window})

        async def fetch_quotes(self, source: Universe) -> ProviderResult:
            del source
            return ProviderResult(quotes=(make_quote("AAA"),))

        async def aclose(self) -> None:
            return None

    registry = ProviderRegistry()
    registry.register(QuotesOnly())
    service = HistoryService(
        universes=YamlUniverseRepository([universe]),
        feed=MarketFeed(providers=registry, cache=TTLCache(60.0)),
        providers=registry,
        cache=TTLCache(60.0),
    )

    with pytest.raises(HistoryUnavailable):
        await service.series(universe_id="equity", symbol="AAA", timeframe=Timeframe.M1)
