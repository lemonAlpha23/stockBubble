from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from tests.conftest import FakeProvider, make_quote

from stockbubble.adapters.cache import TTLCache
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.api.app import create_app
from stockbubble.api.routes import UiSettings
from stockbubble.application.bubbles import BubbleService
from stockbubble.application.feed import MarketFeed
from stockbubble.application.history import HistoryService
from stockbubble.application.registry import ProviderRegistry
from stockbubble.application.services import AppServices
from stockbubble.domain.models import PricePoint

NOW = datetime(2026, 11, 6, 4, 0, tzinfo=UTC)


def build_services(
    universes: YamlUniverseRepository,
    provider: FakeProvider,
) -> AppServices:
    registry = ProviderRegistry()
    registry.register(provider)
    feed = MarketFeed(providers=registry, cache=TTLCache(60.0))
    return AppServices(
        universes=universes,
        providers=registry,
        feed=feed,
        bubble=BubbleService(universes=universes, feed=feed, max_nodes=10, clock=lambda: NOW),
        history=HistoryService(
            universes=universes,
            feed=feed,
            providers=registry,
            cache=TTLCache(60.0),
        ),
    )


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider(
        quotes=(
            make_quote("AAA", market_cap=300.0, volume=300.0, changes={}),
            make_quote("BBB", market_cap=100.0, volume=100.0, changes={}),
        ),
        points=(PricePoint(t=1_762_394_400_000, c=2.0),),
    )


@pytest.fixture
async def client(
    universes: YamlUniverseRepository,
    provider: FakeProvider,
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    app = create_app(
        services=build_services(universes, provider),
        ui=UiSettings(title="Test Board", auto_refresh_seconds=30, max_nodes=10),
        web_dir=tmp_path / "no-web",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as http:
        yield http


async def test_config_exposes_windows_and_bases(client: AsyncClient) -> None:
    response = await client.get("/api/config")
    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Test Board"
    assert [item["value"] for item in body["windows"]] == ["1h", "24h", "7d", "30d"]
    assert [item["value"] for item in body["size_bases"]] == ["market_cap", "volume"]
    assert body["auto_refresh_seconds"] == 30
    assert body["color_clamp_pct"] == 10.0
    assert response.headers["cache-control"] == "no-store"


async def test_universes_lists_known_bubbles(client: AsyncClient) -> None:
    response = await client.get("/api/universes")
    assert response.status_code == 200
    body = response.json()
    assert [item["id"] for item in body] == ["equity", "coins"]
    equity = body[0]
    assert equity["asset_class"] == "us_equity"
    assert equity["instrument_count"] == 2
    assert equity["windows"] == ["24h", "7d"]
    assert body[1]["instrument_count"] == 0


async def test_bubbles_defaults_to_first_universe(client: AsyncClient) -> None:
    response = await client.get("/api/bubbles")
    assert response.status_code == 200
    body = response.json()
    assert body["universe"] == {"id": "equity", "name": "测试美股", "asset_class": "us_equity"}
    assert [node["symbol"] for node in body["nodes"]] == ["AAA", "BBB"]
    assert body["window"] == "24h"
    assert body["size_basis"] == "volume"
    assert body["currency"] == "USD"
    assert body["stats"]["provider_quotes"] == 2
    assert body["stats"]["coverage"] == 1.0
    assert body["total_size"] == pytest.approx(400.0)
    node = body["nodes"][0]
    assert node["rank"] == 1
    assert node["share"] == pytest.approx(0.75)
    assert node["quality"] in {"ok", "partial", "stale"}


async def test_bubbles_rejects_unknown_window(client: AsyncClient) -> None:
    response = await client.get("/api/bubbles", params={"window": "30d"})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "unsupported_window"
    assert "hint" in error


async def test_bubbles_rejects_bad_window_syntax(client: AsyncClient) -> None:
    response = await client.get("/api/bubbles", params={"window": "2y"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unsupported_window"


async def test_bubbles_unknown_universe_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/bubbles", params={"universe": "nope"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "universe_not_found"


async def test_bubbles_limit_must_be_positive(client: AsyncClient) -> None:
    response = await client.get("/api/bubbles", params={"limit": 0})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_history_returns_millisecond_points(client: AsyncClient) -> None:
    response = await client.get("/api/history", params={"symbol": "AAA", "timeframe": "1mo"})
    assert response.status_code == 200
    body = response.json()
    assert body["symbol"] == "AAA"
    assert body["timeframe"] == "1mo"
    assert body["source"] == "fake"
    assert body["points"] == [{"t": 1_762_394_400_000, "c": 2.0}]


async def test_history_unknown_symbol_is_404(client: AsyncClient) -> None:
    response = await client.get("/api/history", params={"symbol": "ZZZ"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "symbol_not_found"


async def test_health_reports_providers_and_universes(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert [provider["id"] for provider in body["providers"]] == ["fake"]
    assert body["providers"][0]["windows"] == ["24h", "7d", "30d"]
    assert body["universes"] == 2


async def test_config_and_health_share_deployment_revision(
    universes: YamlUniverseRepository,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_GIT_COMMIT_SHA", "1234567890abcdef")
    monkeypatch.delenv("BUBBLE_BUILD_SHA", raising=False)
    app = create_app(services=build_services(universes, provider))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        config = (await client.get("/api/config")).json()
        health = (await client.get("/api/health")).json()
    assert config["revision"] == health["revision"] == "1234567890abcdef"
    assert config["version"] == health["version"]


async def test_root_without_web_dir_returns_api_hint(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/api/docs"


async def test_static_assets_are_served_when_present(
    universes: YamlUniverseRepository,
    provider: FakeProvider,
    tmp_path: Path,
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<h1>board</h1>", encoding="utf-8")
    app = create_app(services=build_services(universes, provider), web_dir=web)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        page = await http.get("/")
        assert page.status_code == 200
        assert "board" in page.text
        assert page.headers["cache-control"] == "no-cache"
        api = await http.get("/api/config")
        assert api.headers["cache-control"] == "no-store"
