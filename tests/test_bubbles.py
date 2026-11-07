from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.conftest import FakeProvider, make_quote

from stockbubble.adapters.cache import TTLCache
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.application.bubbles import BubbleService
from stockbubble.application.feed import MarketFeed
from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.errors import UniverseNotFound, UnsupportedWindow
from stockbubble.domain.models import AssetClass, ChangeWindow, SizeBasis, Universe

FIXED_NOW = datetime(2026, 11, 6, 3, 0, tzinfo=UTC)


def build_service(
    universe: Universe,
    provider: FakeProvider,
    *,
    max_nodes: int = 400,
) -> BubbleService:
    registry = ProviderRegistry()
    registry.register(provider)
    feed = MarketFeed(providers=registry, cache=TTLCache(60.0))
    return BubbleService(
        universes=YamlUniverseRepository([universe]),
        feed=feed,
        max_nodes=max_nodes,
        clock=lambda: FIXED_NOW,
    )


async def test_snapshot_uses_universe_defaults(universe: Universe) -> None:
    provider = FakeProvider(
        quotes=(
            make_quote("AAA", market_cap=300.0, volume=20.0),
            make_quote("BBB", market_cap=100.0, volume=80.0),
        )
    )
    service = build_service(universe, provider)
    snapshot = await service.snapshot("equity")

    assert snapshot.universe_id == "equity"
    assert snapshot.universe_name == "测试美股"
    assert snapshot.asset_class is AssetClass.US_EQUITY
    assert snapshot.window is ChangeWindow.H24
    # 默认尺寸口径是成交额，但入选和排名始终按市值。
    assert snapshot.size_basis is SizeBasis.VOLUME
    assert snapshot.currency == "USD"
    assert snapshot.generated_at == FIXED_NOW
    assert [node.symbol for node in snapshot.nodes] == ["AAA", "BBB"]
    assert snapshot.stats.provider_quotes == 2
    assert snapshot.stats.returned == 2
    assert snapshot.stats.coverage == 1.0


async def test_snapshot_rejects_unsupported_window(universe: Universe) -> None:
    service = build_service(universe, FakeProvider(quotes=(make_quote("AAA"),)))
    with pytest.raises(UnsupportedWindow) as excinfo:
        await service.snapshot("equity", window=ChangeWindow.D30)
    assert excinfo.value.status == 422
    assert excinfo.value.hint is not None
    assert "24h" in excinfo.value.hint


async def test_snapshot_unknown_universe(universe: Universe) -> None:
    service = build_service(universe, FakeProvider())
    with pytest.raises(UniverseNotFound):
        await service.snapshot("nope")


async def test_limit_is_capped_by_universe_and_service(universe: Universe) -> None:
    quotes = tuple(
        make_quote(name, market_cap=float(index + 1), volume=float(index + 1))
        for index, name in enumerate("ABCD")
    )

    capped = await build_service(universe, FakeProvider(quotes=quotes)).snapshot("equity")
    assert len(capped.nodes) == 2  # universe.limit
    assert capped.stats.trimmed == 2

    service = build_service(universe, FakeProvider(quotes=quotes), max_nodes=1)
    capped_by_service = await service.snapshot("equity", limit=50)
    assert len(capped_by_service.nodes) == 1
    assert capped_by_service.nodes[0].symbol == "D"


async def test_volume_basis_preserves_market_cap_ordering(universe: Universe) -> None:
    provider = FakeProvider(
        quotes=(
            make_quote("AAA", market_cap=900.0, volume=10.0),
            make_quote("BBB", market_cap=100.0, volume=90.0),
        )
    )
    snapshot = await build_service(universe, provider).snapshot(
        "equity", size_basis=SizeBasis.VOLUME
    )
    assert [node.symbol for node in snapshot.nodes] == ["AAA", "BBB"]
    assert snapshot.nodes[0].size_value == 10.0


async def test_empty_feed_warns_with_no_data(universe: Universe) -> None:
    snapshot = await build_service(universe, FakeProvider()).snapshot("equity")
    assert snapshot.nodes == ()
    assert snapshot.stats.coverage == 0.0
    codes = {(warning.code, warning.severity) for warning in snapshot.warnings}
    assert ("no_data", "error") in codes
