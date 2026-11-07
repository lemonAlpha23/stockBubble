from __future__ import annotations

import pytest
from tests.conftest import FakeProvider, make_quote

from stockbubble.adapters.cache import TTLCache
from stockbubble.application.feed import MarketFeed
from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.errors import ProviderNotFound, ProviderUnavailable
from stockbubble.domain.models import ProviderWarning, Universe


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def build_feed(provider: FakeProvider, clock: Clock, ttl: float = 10.0) -> MarketFeed:
    registry = ProviderRegistry()
    registry.register(provider)
    return MarketFeed(providers=registry, cache=TTLCache(ttl, clock=clock))


async def test_quotes_are_cached_and_reuse_provider_results(universe: Universe) -> None:
    clock = Clock()
    provider = FakeProvider(quotes=(make_quote("AAA"), make_quote("BBB")))
    feed = build_feed(provider, clock)

    first = await feed.quotes(universe)
    second = await feed.quotes(universe)

    assert [quote.symbol for quote in first.quotes] == ["AAA", "BBB"]
    assert first.stale is False
    assert provider.quote_calls == 1
    assert second.age_seconds == pytest.approx(0.0)


async def test_stale_cache_emits_warning_and_keeps_previous_quotes(universe: Universe) -> None:
    clock = Clock()
    provider = FakeProvider(quotes=(make_quote("AAA"),))
    feed = build_feed(provider, clock, ttl=5.0)
    await feed.quotes(universe)

    clock.now += 60.0
    provider.error = ProviderUnavailable("fake", "network down")
    stale = await feed.quotes(universe)

    assert stale.stale is True
    assert [quote.symbol for quote in stale.quotes] == ["AAA"]
    codes = [warning.code for warning in stale.warnings]
    assert "stale_cache" in codes
    assert stale.age_seconds == pytest.approx(60.0)


async def test_provider_warnings_are_passed_through(universe: Universe) -> None:
    provider = FakeProvider(
        quotes=(make_quote("AAA"),),
        warnings=(
            ProviderWarning(
                provider="fake",
                code="partial_coverage",
                message="2 of 3 symbols returned quotes",
            ),
        ),
    )
    feed = build_feed(provider, Clock())
    result = await feed.quotes(universe)
    assert [warning.code for warning in result.warnings] == ["partial_coverage"]
    assert result.stale is False


async def test_unknown_provider_raises(universe: Universe) -> None:
    feed = MarketFeed(providers=ProviderRegistry(), cache=TTLCache(10.0))
    with pytest.raises(ProviderNotFound):
        await feed.quotes(universe)


async def test_cache_key_is_per_universe_and_currency(universe: Universe) -> None:
    assert MarketFeed.cache_key(universe) == "quotes:fake:equity:USD"


async def test_invalidate_drops_cached_entry(universe: Universe) -> None:
    clock = Clock()
    provider = FakeProvider(quotes=(make_quote("AAA"),))
    registry = ProviderRegistry()
    registry.register(provider)
    cache = TTLCache(60.0, clock=clock)
    feed = MarketFeed(providers=registry, cache=cache)

    await feed.quotes(universe)
    await feed.invalidate(universe)
    assert cache.age(MarketFeed.cache_key(universe)) is None
    await feed.quotes(universe)
    assert provider.quote_calls == 2
