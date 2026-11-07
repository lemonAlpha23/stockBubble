"""CoinGecko adapter — crypto quotes and history (no API key required)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from stockbubble.adapters.http import JsonHttpClient
from stockbubble.adapters.support import as_float, downsample, parse_epoch, parse_iso
from stockbubble.domain.errors import ProviderUnavailable, UniverseConfigError
from stockbubble.domain.models import (
    AssetClass,
    ChangeWindow,
    DataQuality,
    PricePoint,
    ProviderResult,
    ProviderWarning,
    Quote,
    Timeframe,
    Universe,
)

DEFAULT_BASE_URL = "https://api.coingecko.com/api/v3"
STALE_AFTER = timedelta(minutes=20)

_CHANGE_FIELDS: Mapping[ChangeWindow, str] = {
    ChangeWindow.H1: "price_change_percentage_1h_in_currency",
    ChangeWindow.H24: "price_change_percentage_24h_in_currency",
    ChangeWindow.D7: "price_change_percentage_7d_in_currency",
    ChangeWindow.D30: "price_change_percentage_30d_in_currency",
}

_SERIES_DAYS: Mapping[Timeframe, int] = {
    Timeframe.D1: 1,
    Timeframe.D5: 7,
    Timeframe.M1: 30,
    Timeframe.M3: 90,
    Timeframe.Y1: 365,
}

MAX_POINTS = 120


class CoinGeckoProvider:
    """Implements ``MarketDataProvider`` and ``PriceSeriesProvider``."""

    provider_id = "coingecko"
    asset_class = AssetClass.CRYPTO
    supported_windows = frozenset(
        {ChangeWindow.H1, ChangeWindow.H24, ChangeWindow.D7, ChangeWindow.D30}
    )

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key: str | None = None,
        timeout: float = 12.0,
        retries: int = 2,
        page_size: int = 250,
        clock: Any = None,
    ) -> None:
        self._client = JsonHttpClient(
            base_url,
            provider=self.provider_id,
            timeout=timeout,
            retries=retries,
        )
        self._api_key = api_key
        self._page_size = max(1, min(page_size, 250))
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def fetch_quotes(self, universe: Universe) -> ProviderResult:
        if universe.asset_class is not AssetClass.CRYPTO:
            msg = f"coingecko cannot serve asset_class {universe.asset_class.value!r}"
            raise UniverseConfigError(msg)

        limit = universe.limit or self._page_size
        wanted = limit + len(universe.exclude)
        collected: list[Quote] = []
        seen = 0
        page = 1
        while seen < wanted and page <= 4:
            rows = await self._markets_page(page, universe.currency)
            if not rows:
                break
            seen += len(rows)
            for raw in rows:
                quote = self._to_quote(raw, universe)
                if quote is not None:
                    collected.append(quote)
            if len(rows) < self._page_size:
                break
            page += 1

        excluded = {symbol.strip().upper() for symbol in universe.exclude}
        kept = [quote for quote in collected if quote.symbol not in excluded][:limit]

        warnings: list[ProviderWarning] = []
        if not kept:
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="empty_payload",
                    message="CoinGecko returned no usable quotes",
                    severity="error",
                )
            )
        return ProviderResult(quotes=tuple(kept), warnings=tuple(warnings))

    async def fetch_series(
        self,
        *,
        external_id: str,
        timeframe: Timeframe,
        currency: str,
    ) -> list[PricePoint]:
        days = _SERIES_DAYS[timeframe]
        payload = await self._client.get_json(
            f"/coins/{external_id}/market_chart",
            params={"vs_currency": currency.lower(), "days": days},
        )
        prices = payload.get("prices") if isinstance(payload, dict) else None
        if not isinstance(prices, list):
            raise ProviderUnavailable(self.provider_id, "market_chart payload missing 'prices'")
        points: list[PricePoint] = []
        for item in prices:
            if isinstance(item, list | tuple) and len(item) >= 2:
                stamp = parse_epoch(_per_second(item[0]))
                close = as_float(item[1])
                if stamp is not None and close is not None:
                    points.append(PricePoint(t=stamp, c=close))
        if not points:
            raise ProviderUnavailable(self.provider_id, "market_chart returned no points")
        return downsample(points, MAX_POINTS)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _markets_page(self, page: int, currency: str) -> list[Any]:
        payload = await self._client.get_json(
            "/coins/markets",
            params={
                "vs_currency": currency.lower(),
                "order": "market_cap_desc",
                "per_page": self._page_size,
                "page": page,
                "sparkline": "false",
                "price_change_percentage": "1h,24h,7d,30d",
            },
            headers=self._auth_headers(),
        )
        if not isinstance(payload, list):
            raise ProviderUnavailable(self.provider_id, "coins/markets payload is not a list")
        return payload

    def _auth_headers(self) -> dict[str, str] | None:
        if not self._api_key:
            return None
        return {"x-cg-demo-api-key": self._api_key}

    def _to_quote(self, raw: Any, universe: Universe) -> Quote | None:
        if not isinstance(raw, dict):
            return None
        symbol = str(raw.get("symbol") or "").strip().upper()
        if not symbol:
            return None
        price = as_float(raw.get("current_price"))
        if price is None:
            return None
        changes = {window: as_float(raw.get(field)) for window, field in _CHANGE_FIELDS.items()}
        as_of = parse_iso(raw.get("last_updated")) or self._clock()
        quality = DataQuality.STALE if self._clock() - as_of > STALE_AFTER else DataQuality.OK
        rank = as_float(raw.get("market_cap_rank"))
        return Quote(
            symbol=symbol,
            name=str(raw.get("name") or symbol),
            asset_class=AssetClass.CRYPTO,
            price=price,
            currency=universe.currency,
            changes=changes,
            market_cap=as_float(raw.get("market_cap")),
            volume=as_float(raw.get("total_volume")),
            sector=universe.sector_of(symbol),
            rank=int(rank) if rank is not None else None,
            external_id=str(raw.get("id") or "") or None,
            logo_url=str(raw.get("image") or "") or None,
            source=self.provider_id,
            as_of=as_of,
            quality=quality,
        )


def _per_second(value: Any) -> Any:
    number = as_float(value)
    if number is None:
        return None
    return number / 1000.0
