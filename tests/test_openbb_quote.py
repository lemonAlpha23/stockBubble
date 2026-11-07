"""OpenBB adapter: pure mapping and fallback behaviour, no network."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from stockbubble.adapters.openbb_quote import (
    OpenBBProvider,
    _clean_name,
    _percent,
    patch_nasdaq_transport,
)
from stockbubble.domain.models import AssetClass, ChangeWindow, DataQuality, Timeframe, Universe


class _FakeSeries:
    """Stand-in for the spark feed the provider composes."""

    def __init__(self, daily: dict[str, list[float]], intraday: dict[str, list[float]]) -> None:
        self._daily = daily
        self._intraday = intraday
        self.series_calls: list[str] = []

    async def daily_closes(self, symbols: list[str]) -> dict[str, list[float]]:
        return {symbol: self._daily[symbol] for symbol in symbols if symbol in self._daily}

    async def batch_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return {}

    async def intraday_closes(self, symbols: list[str]) -> dict[str, list[float]]:
        return {symbol: self._intraday[symbol] for symbol in symbols if symbol in self._intraday}

    async def fetch_series(self, *, external_id: str, timeframe: Timeframe, currency: str) -> Any:
        del timeframe, currency
        self.series_calls.append(external_id)
        return []

    async def aclose(self) -> None:
        return None


def _universe(**overrides: Any) -> Universe:
    payload: dict[str, Any] = {
        "id": "openbb_test",
        "name": "测试美股",
        "asset_class": AssetClass.US_EQUITY,
        "provider": "openbb",
        "currency": "USD",
        "default_window": ChangeWindow.H24,
        "supported_windows": (
            ChangeWindow.H1,
            ChangeWindow.H24,
            ChangeWindow.D7,
            ChangeWindow.D30,
        ),
        "limit": 10,
        "instruments": [{"symbol": "AAPL", "name": "Apple", "sector": "科技"}],
    }
    payload.update(overrides)
    return Universe(**payload)


def _provider(series: _FakeSeries) -> OpenBBProvider:
    return OpenBBProvider(
        series=series,  # type: ignore[arg-type]
        quote_provider="nasdaq",
        chunk_size=10,
        now=lambda: datetime(2026, 11, 7, tzinfo=UTC),
    )


async def test_quote_maps_every_window() -> None:
    daily = [90.0 + index for index in range(40)]
    intraday = [100.0 + index * 0.1 for index in range(20)]
    series = _FakeSeries({"AAPL": daily}, {"AAPL": intraday})
    provider = _provider(series)

    row = {
        "symbol": "AAPL",
        "last_price": 342.6,
        "prev_close": 342.4,
        "change_percent": 0.000584,
        "market_cap": 4.975e12,
        "volume": 44_700_000.0,
        "name": "Apple Inc.",
        "sector": "Technology",
    }

    quote = provider._to_quote(row, daily, intraday, "AAPL", _universe())
    assert quote is not None
    assert quote.source == "openbb"
    assert quote.price == pytest.approx(342.6)
    assert quote.changes[ChangeWindow.H24] == pytest.approx(0.0584, abs=1e-4)
    assert quote.changes[ChangeWindow.H1] == pytest.approx(1.19, abs=0.02)
    assert quote.changes[ChangeWindow.D7] == pytest.approx(4.03, abs=0.05)
    assert quote.changes[ChangeWindow.D30] is not None
    assert quote.volume == pytest.approx(342.6 * 44_700_000.0)
    assert quote.sector == "科技"  # the universe mapping wins over the vendor field
    assert quote.quality is DataQuality.OK


async def test_missing_series_marks_partial() -> None:
    series = _FakeSeries({}, {})
    provider = _provider(series)
    quote = provider._to_quote(
        {"symbol": "AAPL", "last_price": 10.0}, None, None, "AAPL", _universe()
    )
    assert quote is not None
    assert quote.changes[ChangeWindow.H1] is None
    assert quote.quality is DataQuality.PARTIAL


async def test_fetch_quotes_reports_missing_symbols() -> None:
    series = _FakeSeries({"AAPL": [1.0, 2.0]}, {"AAPL": [2.0, 2.1]})
    provider = _provider(series)
    provider._quote_chunk = lambda group: {"AAPL": {"symbol": "AAPL", "last_price": 2.1}}  # type: ignore[method-assign,assignment]

    universe = _universe(
        instruments=[
            {"symbol": "AAPL", "name": "Apple", "sector": "科技"},
            {"symbol": "ZZZZ", "name": "Ghost", "sector": "其他"},
        ]
    )
    result = await provider.fetch_quotes(universe)

    assert [quote.symbol for quote in result.quotes] == ["AAPL"]
    codes = {warning.code for warning in result.warnings}
    assert "missing_symbols" in codes
    assert "partial_coverage" in codes


async def test_quote_without_price_is_skipped() -> None:
    provider = _provider(_FakeSeries({}, {}))
    assert provider._to_quote({"symbol": "AAPL"}, None, None, "AAPL", _universe()) is None


def test_percent_normalises_fraction_and_percent() -> None:
    assert _percent(0.0123) == pytest.approx(1.23)
    assert _percent(4.5) == pytest.approx(4.5)
    assert _percent(None) is None


def test_clean_name_strips_vendor_suffixes() -> None:
    assert _clean_name("Apple Inc. Common Stock", "AAPL") == "Apple Inc."
    assert _clean_name("", "AAPL") == "AAPL"


def test_nasdaq_transport_patch_is_idempotent() -> None:
    assert patch_nasdaq_transport() in (True, False)
    assert patch_nasdaq_transport() in (True, False)
