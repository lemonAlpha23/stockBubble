from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from stockbubble.adapters.logos import StockLogos
from stockbubble.adapters.yahoo import YahooProvider
from stockbubble.domain.errors import ProviderUnavailable, UniverseConfigError
from stockbubble.domain.models import (
    AssetClass,
    ChangeWindow,
    DataQuality,
    Instrument,
    Timeframe,
    Universe,
)

BASE = "https://query1.finance.yahoo.com"
CRUMB = f"{BASE}/v1/test/getcrumb"
QUOTES = f"{BASE}/v7/finance/quote"
SPARK = f"{BASE}/v8/finance/spark"
CHART = f"{BASE}/v8/finance/chart/AAA"
FC = "https://fc.yahoo.com"

MARKET_TIME = 1_762_394_400  # 2025-11-06T02:00:00Z
NOW = datetime(2025, 11, 6, 3, 0, tzinfo=UTC)


def quote_row(symbol: str, *, price: float = 100.0, change_pct: float = 1.5) -> dict[str, object]:
    return {
        "symbol": symbol,
        "shortName": f"{symbol} Inc.",
        "regularMarketPrice": price,
        "regularMarketChangePercent": change_pct,
        "regularMarketVolume": 1_000,
        "marketCap": 5_000.0,
        "regularMarketTime": MARKET_TIME,
        "currency": "USD",
    }


def close_series(count: int = 30) -> list[float]:
    return [100.0 + index for index in range(count)]


def mock_crumb() -> None:
    respx.get(FC).mock(return_value=httpx.Response(404, text="not found"))
    respx.get(CRUMB).mock(return_value=httpx.Response(200, text="crumb-token"))


def build_provider(**kwargs: object) -> YahooProvider:
    defaults: dict[str, object] = {"base_url": BASE, "now": lambda: NOW, "logos": StockLogos()}
    defaults.update(kwargs)
    return YahooProvider(**defaults)  # type: ignore[arg-type]


@respx.mock
async def test_quotes_combine_quote_and_spark(universe: Universe) -> None:
    mock_crumb()
    respx.get(QUOTES).mock(
        return_value=httpx.Response(
            200,
            json={"quoteResponse": {"result": [quote_row("AAA"), quote_row("BBB", price=50.0)]}},
        )
    )
    respx.get(SPARK).mock(
        return_value=httpx.Response(
            200,
            json={"AAA": {"close": close_series()}, "BBB": {"close": close_series(10)}},
        )
    )
    three_windows = universe.model_copy(
        update={"supported_windows": (ChangeWindow.H24, ChangeWindow.D7, ChangeWindow.D30)}
    )

    result = await build_provider().fetch_quotes(three_windows)

    assert [quote.symbol for quote in result.quotes] == ["AAA", "BBB"]
    first = result.quotes[0]
    assert first.price == 100.0
    assert first.volume == 100_000.0  # volume * price
    assert first.market_cap == 5_000.0
    closes = close_series()
    # 1h comes from the 5-minute feed, which the mock serves with the same series.
    assert first.change(ChangeWindow.H1) == pytest.approx((closes[-1] / closes[-13] - 1) * 100)
    assert first.change(ChangeWindow.H24) == 1.5
    expected_7d = (closes[-1] / closes[-6] - 1) * 100
    expected_30d = (closes[-1] / closes[-22] - 1) * 100
    assert first.change(ChangeWindow.D7) == pytest.approx(expected_7d)
    assert first.change(ChangeWindow.D30) == pytest.approx(expected_30d)
    assert first.quality is DataQuality.OK
    # BBB has only 10 closes: 30d cannot be computed, so quality degrades.
    assert result.quotes[1].change(ChangeWindow.D30) is None
    assert result.quotes[1].quality is DataQuality.PARTIAL
    assert result.warnings == ()
    # 美股气泡也要有公司图标：主源 + 兜底源都随行情一起下发
    assert first.logo_url == "https://assets.parqet.com/logos/symbol/AAA?format=png"
    assert first.logo_fallback == "https://companiesmarketcap.com/img/company-logos/64/AAA.png"


@respx.mock
async def test_missing_symbols_and_coverage_warnings(universe: Universe) -> None:
    mock_crumb()
    respx.get(QUOTES).mock(
        return_value=httpx.Response(200, json={"quoteResponse": {"result": [quote_row("AAA")]}})
    )
    respx.get(SPARK).mock(return_value=httpx.Response(200, json={"AAA": {"close": close_series()}}))

    result = await build_provider().fetch_quotes(universe)

    codes = [warning.code for warning in result.warnings]
    assert "missing_symbols" in codes
    assert "partial_coverage" in codes


@respx.mock
async def test_spark_failure_keeps_quotes_and_nulls_multi_day_windows(universe: Universe) -> None:
    mock_crumb()
    respx.get(QUOTES).mock(
        return_value=httpx.Response(
            200,
            json={"quoteResponse": {"result": [quote_row("AAA"), quote_row("BBB")]}},
        )
    )
    respx.get(SPARK).mock(return_value=httpx.Response(400, json={"spark": {"result": None}}))

    result = await build_provider().fetch_quotes(universe)

    assert len(result.quotes) == 2
    assert result.quotes[0].change(ChangeWindow.D7) is None
    assert result.quotes[0].quality is DataQuality.PARTIAL


@respx.mock
async def test_crumb_is_refreshed_once_on_401(universe: Universe) -> None:
    mock_crumb()
    respx.get(QUOTES).mock(
        side_effect=[
            httpx.Response(401, text="unauthorized"),
            httpx.Response(200, json={"quoteResponse": {"result": [quote_row("AAA")]}}),
        ]
    )
    respx.get(SPARK).mock(return_value=httpx.Response(200, json={"AAA": {"close": close_series()}}))

    result = await build_provider().fetch_quotes(universe)

    assert [quote.symbol for quote in result.quotes] == ["AAA"]
    assert respx.calls.call_count >= 4  # cookie + crumb + 401 + retry


@respx.mock
async def test_crumb_failure_raises(universe: Universe) -> None:
    respx.get(FC).mock(return_value=httpx.Response(404, text="not found"))
    respx.get(CRUMB).mock(return_value=httpx.Response(401, text="<html>denied</html>"))

    with pytest.raises(ProviderUnavailable, match="crumb"):
        await build_provider().fetch_quotes(universe)


@respx.mock
async def test_quote_http_error_raises(universe: Universe) -> None:
    mock_crumb()
    respx.get(QUOTES).mock(return_value=httpx.Response(500, text="boom"))

    with pytest.raises(ProviderUnavailable, match="HTTP 500"):
        await build_provider(retries=0).fetch_quotes(universe)


@respx.mock
async def test_spark_batches_by_twenty(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_crumb()
    symbols = tuple(f"S{index:02d}" for index in range(25))
    symbols_universe = Universe(
        id="equity",
        name="美股",
        asset_class=AssetClass.US_EQUITY,
        provider="yahoo",
        instruments=tuple(Instrument(symbol=symbol, name=symbol) for symbol in symbols),
    )
    respx.get(QUOTES).mock(
        return_value=httpx.Response(
            200,
            json={"quoteResponse": {"result": [quote_row(symbol) for symbol in symbols]}},
        )
    )
    spark_route = respx.get(SPARK).mock(
        return_value=httpx.Response(
            200, json={symbol: {"close": close_series()} for symbol in symbols}
        )
    )

    await build_provider().fetch_quotes(symbols_universe)

    ranges = [call.request.url.params.get("range") for call in spark_route.calls]
    # 25 symbols -> two groups of 20 per feed, and two feeds (daily + intraday).
    assert ranges.count("1mo") == 2
    assert ranges.count("1d") == 2


async def test_rejects_static_equity_without_instruments(universe: Universe) -> None:
    dynamic = universe.model_copy(update={"instruments": ()})
    with pytest.raises(UniverseConfigError):
        await build_provider().fetch_quotes(dynamic)


async def test_rejects_crypto_universe(crypto_universe: Universe) -> None:
    with pytest.raises(UniverseConfigError):
        await build_provider().fetch_quotes(crypto_universe)


@respx.mock
async def test_series_reads_chart_payload() -> None:
    mock_crumb()
    respx.get(CHART).mock(
        return_value=httpx.Response(
            200,
            json={
                "chart": {
                    "result": [
                        {
                            "timestamp": [MARKET_TIME, MARKET_TIME + 86_400],
                            "indicators": {"quote": [{"close": [100.0, None]}]},
                        }
                    ]
                }
            },
        )
    )
    points = await build_provider().fetch_series(
        external_id="AAA",
        timeframe=Timeframe.M1,
        currency="USD",
    )
    assert [point.c for point in points] == [100.0]
    assert points[0].t == datetime(2025, 11, 6, 2, 0, tzinfo=UTC)


@respx.mock
async def test_series_without_result_raises() -> None:
    mock_crumb()
    respx.get(CHART).mock(return_value=httpx.Response(200, json={"chart": {"result": []}}))
    with pytest.raises(ProviderUnavailable, match="empty"):
        await build_provider().fetch_series(
            external_id="AAA",
            timeframe=Timeframe.D1,
            currency="USD",
        )
