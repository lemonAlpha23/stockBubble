"""Shared fixtures: fake providers and a two-universe repository.

Tests never touch the network. Every fake here satisfies the domain protocols
structurally, which also proves the ports are usable without inheritance.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.domain.models import (
    AssetClass,
    ChangeWindow,
    DataQuality,
    Instrument,
    PricePoint,
    ProviderResult,
    ProviderWarning,
    Quote,
    Timeframe,
    Universe,
)

NOW = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)


def make_quote(symbol: str, **overrides: object) -> Quote:
    """A valid US-equity quote with overridable fields."""
    defaults: dict[str, object] = {
        "symbol": symbol,
        "name": f"{symbol} Holdings",
        "asset_class": AssetClass.US_EQUITY,
        "price": 100.0,
        "currency": "USD",
        "changes": dict.fromkeys(ChangeWindow, 1.0),
        "market_cap": 1_000.0,
        "volume": 10.0,
        "sector": "科技",
        "external_id": symbol,
        "source": "fake",
        "as_of": NOW,
        "quality": DataQuality.OK,
    }
    defaults.update(overrides)
    return Quote(**defaults)


class FakeProvider:
    """MarketDataProvider + PriceSeriesProvider double."""

    provider_id = "fake"
    asset_class = AssetClass.US_EQUITY
    supported_windows = frozenset({ChangeWindow.H24, ChangeWindow.D7, ChangeWindow.D30})

    def __init__(
        self,
        *,
        quotes: tuple[Quote, ...] = (),
        warnings: tuple[ProviderWarning, ...] = (),
        points: tuple[PricePoint, ...] = (),
        error: Exception | None = None,
    ) -> None:
        self.quotes = quotes
        self.warnings = warnings
        self.points = points
        self.error = error
        self.quote_calls = 0
        self.series_calls = 0
        self.closed = False

    async def fetch_quotes(self, universe: Universe) -> ProviderResult:
        del universe
        self.quote_calls += 1
        if self.error is not None:
            raise self.error
        return ProviderResult(quotes=self.quotes, warnings=self.warnings)

    async def fetch_series(
        self,
        *,
        external_id: str,
        timeframe: Timeframe,
        currency: str,
    ) -> list[PricePoint]:
        del external_id, timeframe, currency
        self.series_calls += 1
        if self.error is not None:
            raise self.error
        return list(self.points)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def universe() -> Universe:
    return Universe(
        id="equity",
        name="测试美股",
        asset_class=AssetClass.US_EQUITY,
        provider="fake",
        currency="USD",
        default_window=ChangeWindow.H24,
        supported_windows=(ChangeWindow.H24, ChangeWindow.D7),
        limit=2,
        instruments=(
            Instrument(symbol="AAA", name="A 公司", sector="科技"),
            Instrument(symbol="BBB", name="B 公司", sector="金融"),
        ),
    )


@pytest.fixture
def crypto_universe() -> Universe:
    return Universe(
        id="coins",
        name="测试加密货币",
        asset_class=AssetClass.CRYPTO,
        provider="fake",
        default_window=ChangeWindow.H24,
        supported_windows=(ChangeWindow.H1, ChangeWindow.H24, ChangeWindow.D7),
        limit=3,
        sectors={"AAA": "公链"},
        exclude=("USDT",),
    )


@pytest.fixture
def universes(universe: Universe, crypto_universe: Universe) -> YamlUniverseRepository:
    return YamlUniverseRepository([universe, crypto_universe])
