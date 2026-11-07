from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx

from stockbubble.adapters.coingecko import CoinGeckoProvider
from stockbubble.domain.errors import ProviderRateLimited, ProviderUnavailable, UniverseConfigError
from stockbubble.domain.models import AssetClass, ChangeWindow, DataQuality, Timeframe, Universe

BASE = "https://api.coingecko.com/api/v3"
NOW = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)


def make_row(
    symbol: str,
    *,
    coin_id: str | None = None,
    price: float = 10.0,
    name: str | None = None,
    updated: datetime | None = None,
    changes: tuple[float | None, ...] = (1.0, 2.0, 3.0, 4.0),
) -> dict[str, object]:
    stamp = updated or NOW
    return {
        "id": coin_id or symbol.lower(),
        "symbol": symbol,
        "name": name or f"{symbol} Coin",
        "current_price": price,
        "market_cap": price * 1_000,
        "market_cap_rank": 7,
        "image": f"https://cdn.example/{symbol.lower()}.png",
        "total_volume": price * 100,
        "last_updated": stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "price_change_percentage_1h_in_currency": changes[0],
        "price_change_percentage_24h_in_currency": changes[1],
        "price_change_percentage_7d_in_currency": changes[2],
        "price_change_percentage_30d_in_currency": changes[3],
    }


def build_provider(**kwargs: object) -> CoinGeckoProvider:
    defaults: dict[str, object] = {"base_url": BASE, "clock": lambda: NOW}
    defaults.update(kwargs)
    return CoinGeckoProvider(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def crypto_universe(crypto_universe: Universe) -> Universe:
    return crypto_universe.model_copy(
        update={"limit": 5, "exclude": ("USDT",), "sectors": {"BTC": "公链"}}
    )


@respx.mock
async def test_quotes_are_normalised_and_excluded(crypto_universe: Universe) -> None:
    respx.get(f"{BASE}/coins/markets").mock(
        return_value=httpx.Response(
            200,
            json=[
                make_row("BTC", price=60_000.0),
                make_row("USDT", price=1.0),
                make_row("ETH", price=3_000.0, coin_id="ethereum"),
            ],
        )
    )
    provider = build_provider()
    result = await provider.fetch_quotes(crypto_universe)

    assert [quote.symbol for quote in result.quotes] == ["BTC", "ETH"]
    assert result.warnings == ()
    btc = result.quotes[0]
    assert btc.asset_class is AssetClass.CRYPTO
    assert btc.price == 60_000.0
    assert btc.market_cap == 60_000_000.0
    assert btc.volume == 6_000_000.0
    assert btc.rank == 7
    assert btc.sector == "公链"  # from the universe sector map
    assert btc.external_id == "btc"
    assert btc.logo_url == "https://cdn.example/btc.png"
    assert btc.quality is DataQuality.OK
    assert btc.change(ChangeWindow.H24) == 2.0
    assert btc.change(ChangeWindow.D30) == 4.0
    assert result.quotes[1].external_id == "ethereum"


@respx.mock
async def test_old_quotes_are_marked_stale(crypto_universe: Universe) -> None:
    stale_moment = datetime(2026, 11, 6, 1, 0, tzinfo=UTC)  # 3h before the fixed clock
    respx.get(f"{BASE}/coins/markets").mock(
        return_value=httpx.Response(200, json=[make_row("BTC", updated=stale_moment)])
    )
    result = await build_provider().fetch_quotes(crypto_universe)
    assert result.quotes[0].quality is DataQuality.STALE


@respx.mock
async def test_pagination_stops_when_limit_is_reached(crypto_universe: Universe) -> None:
    route = respx.get(f"{BASE}/coins/markets").mock(
        side_effect=[
            httpx.Response(200, json=[make_row("AAA"), make_row("BBB")]),
            httpx.Response(200, json=[make_row("CCC"), make_row("DDD")]),
            httpx.Response(200, json=[make_row("EEE"), make_row("FFF")]),
        ]
    )
    provider = build_provider(page_size=2)
    result = await provider.fetch_quotes(crypto_universe.model_copy(update={"limit": 1}))

    assert [quote.symbol for quote in result.quotes] == ["AAA"]
    assert route.call_count == 1
    assert route.calls[0].request.url.params["page"] == "1"


@respx.mock
async def test_empty_payload_warns(crypto_universe: Universe) -> None:
    respx.get(f"{BASE}/coins/markets").mock(return_value=httpx.Response(200, json=[]))
    result = await build_provider().fetch_quotes(crypto_universe)
    assert result.quotes == ()
    assert [warning.code for warning in result.warnings] == ["empty_payload"]
    assert result.warnings[0].severity == "error"


@respx.mock
async def test_non_list_payload_raises(crypto_universe: Universe) -> None:
    respx.get(f"{BASE}/coins/markets").mock(
        return_value=httpx.Response(200, json={"status": {"error": "rate limited"}})
    )
    with pytest.raises(ProviderUnavailable):
        await build_provider().fetch_quotes(crypto_universe)


async def test_rejects_non_crypto_universe(universe: Universe) -> None:
    with pytest.raises(UniverseConfigError):
        await build_provider().fetch_quotes(universe)


@respx.mock
async def test_api_key_is_sent_as_header(crypto_universe: Universe) -> None:
    route = respx.get(f"{BASE}/coins/markets").mock(
        return_value=httpx.Response(200, json=[make_row("BTC")])
    )
    await build_provider(api_key="demo-key").fetch_quotes(crypto_universe)
    assert route.calls[0].request.headers["x-cg-demo-api-key"] == "demo-key"


@respx.mock
async def test_series_parses_millisecond_stamps(crypto_universe: Universe) -> None:
    respx.get(f"{BASE}/coins/bitcoin/market_chart").mock(
        return_value=httpx.Response(
            200,
            json={
                "prices": [
                    [1_762_394_400_000, 60_000.0],
                    [1_762_480_800_000, 61_000.0],
                ]
            },
        )
    )
    points = await build_provider().fetch_series(
        external_id="bitcoin",
        timeframe=Timeframe.D1,
        currency="USD",
    )
    assert [point.c for point in points] == [60_000.0, 61_000.0]
    assert points[0].t == datetime(2025, 11, 6, 2, 0, tzinfo=UTC)


@respx.mock
async def test_series_without_prices_raises(crypto_universe: Universe) -> None:
    respx.get(f"{BASE}/coins/bitcoin/market_chart").mock(
        return_value=httpx.Response(200, json={"market_caps": []})
    )
    with pytest.raises(ProviderUnavailable):
        await build_provider().fetch_series(
            external_id="bitcoin",
            timeframe=Timeframe.M1,
            currency="USD",
        )


@respx.mock
async def test_year_series_sends_demo_key_and_365_days() -> None:
    route = respx.get(f"{BASE}/coins/bitcoin/market_chart").mock(
        return_value=httpx.Response(200, json={"prices": [[1_762_394_400_000, 60_000.0]]})
    )
    provider = build_provider(api_key="demo-key")
    await provider.fetch_series(external_id="bitcoin", timeframe=Timeframe.Y1, currency="USD")
    assert route.calls[0].request.url.params["days"] == "365"
    assert route.calls[0].request.headers["x-cg-demo-api-key"] == "demo-key"
    await provider.aclose()


@respx.mock
async def test_rate_limit_cools_down_without_retrying_or_switching_ranges(
    crypto_universe: Universe,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr("stockbubble.adapters.http.monotonic", lambda: now[0])
    route = respx.get(f"{BASE}/coins/bitcoin/market_chart").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200, json={"prices": [[1_762_394_400_000, 60_000.0]]}),
        ]
    )
    markets = respx.get(f"{BASE}/coins/markets").mock(return_value=httpx.Response(200, json=[]))
    provider = build_provider(retries=2)
    with pytest.raises(ProviderRateLimited) as exc:
        await provider.fetch_series(external_id="bitcoin", timeframe=Timeframe.Y1, currency="USD")
    assert exc.value.retry_after == 120
    assert route.call_count == 1
    with pytest.raises(ProviderRateLimited):
        await provider.fetch_series(external_id="bitcoin", timeframe=Timeframe.M1, currency="USD")
    with pytest.raises(ProviderRateLimited):
        await provider.fetch_quotes(crypto_universe)
    assert route.call_count == 1
    assert markets.call_count == 0
    now[0] += 121
    points = await provider.fetch_series(
        external_id="bitcoin", timeframe=Timeframe.Y1, currency="USD"
    )
    assert points[0].c == 60_000.0
    assert route.call_count == 2
    await provider.aclose()
