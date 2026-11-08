"""Yahoo Finance adapter — US equity quotes, multi-window changes and history.

Yahoo's public endpoints require a cookie + ``crumb`` pair. This adapter keeps a
small in-memory crumb cache, refreshes it on ``401/403`` and never persists it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from stockbubble.adapters.http import JsonHttpClient
from stockbubble.adapters.logos import StockLogos
from stockbubble.adapters.support import as_float, chunks, downsample, parse_epoch, pct_change
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

LOGGER = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://query1.finance.yahoo.com"
CRUMB_TTL_SECONDS = 6 * 3600.0
STALE_AFTER = timedelta(days=7)

#: Which slice of the daily close series represents each window.
_WINDOW_SESSIONS: Mapping[ChangeWindow, int] = {
    ChangeWindow.D7: 5,
    ChangeWindow.D30: 21,
}

#: 5-minute bars in one hour — the intraday series gives the 1h window.
INTRADAY_INTERVAL = "5m"
INTRADAY_BARS_1H = 12

_SERIES_RANGE: Mapping[Timeframe, tuple[str, str]] = {
    Timeframe.D1: ("1d", "5m"),
    Timeframe.D5: ("5d", "30m"),
    Timeframe.M1: ("1mo", "1d"),
    Timeframe.M3: ("3mo", "1d"),
    Timeframe.Y1: ("1y", "1wk"),
}

MAX_POINTS = 180

#: ``/v8/finance/spark`` rejects more than 20 symbols per request (HTTP 400).
SPARK_BATCH_SIZE = 20


class YahooProvider:
    """US equity quotes via ``/v7/finance/quote`` + ``/v8/finance/spark``."""

    provider_id = "yahoo"
    asset_class = AssetClass.US_EQUITY
    supported_windows = frozenset(
        {ChangeWindow.H1, ChangeWindow.H24, ChangeWindow.D7, ChangeWindow.D30}
    )

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        asset_class: AssetClass = AssetClass.US_EQUITY,
        provider_id: str = "yahoo",
        timeout: float = 12.0,
        retries: int = 2,
        batch_size: int = 50,
        logos: StockLogos | None = None,
        clock: Any = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.asset_class = asset_class
        self.provider_id = provider_id
        self._logos = logos
        self._client = JsonHttpClient(
            base_url,
            provider=self.provider_id,
            timeout=timeout,
            retries=retries,
            # Yahoo's crumb endpoint answers 406 unless the browser-ish Accept set
            # is used; ``application/json`` is explicitly rejected there.
            headers={
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://finance.yahoo.com/",
                "Origin": "https://finance.yahoo.com",
            },
        )
        self._batch_size = max(1, batch_size)
        self._clock = clock or time.monotonic
        self._now_fn = now or (lambda: datetime.now(tz=UTC))
        self._crumb: str | None = None
        self._crumb_at = 0.0
        self._crumb_lock = asyncio.Lock()

    def _now(self) -> datetime:
        return self._now_fn()

    async def fetch_quotes(self, universe: Universe) -> ProviderResult:
        if universe.asset_class is not self.asset_class:
            msg = f"yahoo cannot serve asset_class {universe.asset_class.value!r}"
            raise UniverseConfigError(msg)
        symbols = list(universe.symbols())
        if not symbols:
            msg = f"universe {universe.id!r} has no static instruments for yahoo"
            raise UniverseConfigError(msg)

        quotes: list[Quote] = []
        warnings: list[ProviderWarning] = []
        for batch in chunks(symbols, self._batch_size):
            rows = await self._quotes(batch)
            closes = await self.daily_closes(batch)
            intraday = await self.intraday_closes(batch)
            for symbol in batch:
                quote = self._to_quote(
                    rows.get(symbol),
                    closes.get(symbol),
                    intraday.get(symbol),
                    symbol,
                    universe,
                )
                if quote is not None:
                    quotes.append(quote)
            missing = [symbol for symbol in batch if symbol not in rows]
            if missing:
                warnings.append(
                    ProviderWarning(
                        provider=self.provider_id,
                        code="missing_symbols",
                        message=f"no quote for {len(missing)} symbol(s): {', '.join(missing[:8])}",
                        severity="warning",
                    )
                )

        warnings.extend(self._cadence_warnings(universe, len(quotes)))
        return ProviderResult(quotes=tuple(quotes), warnings=tuple(warnings))

    async def fetch_series(
        self,
        *,
        external_id: str,
        timeframe: Timeframe,
        currency: str,
    ) -> list[PricePoint]:
        del currency  # Yahoo reports the listing currency in the payload itself
        span, interval = _SERIES_RANGE[timeframe]
        payload = await self._client.get_json(
            f"/v8/finance/chart/{external_id}",
            params={"range": span, "interval": interval, "includePrePost": "false"},
        )
        result = _first_result(payload)
        if result is None:
            raise ProviderUnavailable(self.provider_id, f"chart payload for {external_id} is empty")
        stamps = result.get("timestamp")
        indicators = result.get("indicators")
        quote_block = None
        if isinstance(indicators, dict):
            raw_quotes = indicators.get("quote")
            if isinstance(raw_quotes, list) and raw_quotes and isinstance(raw_quotes[0], dict):
                quote_block = raw_quotes[0]
        closes = quote_block.get("close") if quote_block else None
        if not isinstance(stamps, list) or not isinstance(closes, list):
            raise ProviderUnavailable(
                self.provider_id, f"chart payload for {external_id} is malformed"
            )
        points: list[PricePoint] = []
        for stamp, close in zip(stamps, closes, strict=False):
            moment = parse_epoch(stamp)
            value = as_float(close)
            if moment is not None and value is not None:
                points.append(PricePoint(t=moment, c=value))
        if not points:
            raise ProviderUnavailable(self.provider_id, f"no closes for {external_id}")
        return downsample(points, MAX_POINTS)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ http

    async def _authorized(self, path: str, params: Mapping[str, str]) -> Any:
        for attempt in (1, 2):
            crumb = await self._ensure_crumb()
            response = await self._client.request("GET", path, params={**params, "crumb": crumb})
            if response.status_code in (401, 403) and attempt == 1:
                self._crumb = None
                continue
            if response.status_code >= 400:
                snippet = response.text[:160].replace("\n", " ")
                raise ProviderUnavailable(
                    self.provider_id,
                    f"GET {path} -> HTTP {response.status_code}: {snippet}",
                )
            try:
                return response.json()
            except ValueError as exc:
                raise ProviderUnavailable(
                    self.provider_id, f"GET {path} returned non-JSON payload"
                ) from exc
        raise ProviderUnavailable(self.provider_id, f"GET {path} rejected the session crumb")

    async def _ensure_crumb(self) -> str:
        if self._crumb and self._clock() - self._crumb_at < CRUMB_TTL_SECONDS:
            return self._crumb
        async with self._crumb_lock:
            if self._crumb and self._clock() - self._crumb_at < CRUMB_TTL_SECONDS:
                return self._crumb
            with contextlib.suppress(ProviderUnavailable, httpx.HTTPError):
                # The 404 from fc.yahoo.com still seeds the consent cookie we need.
                await self._client.request("GET", "https://fc.yahoo.com")
            response = await self._client.request("GET", "/v1/test/getcrumb")
            text = response.text.strip()
            if response.status_code >= 400 or not text or "<" in text or len(text) > 48:
                raise ProviderUnavailable(
                    self.provider_id,
                    f"cannot obtain session crumb (HTTP {response.status_code})",
                )
            self._crumb = text
            self._crumb_at = self._clock()
            return text

    async def _quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        payload = await self._authorized("/v7/finance/quote", {"symbols": ",".join(symbols)})
        if not isinstance(payload, dict):
            raise ProviderUnavailable(self.provider_id, "quote payload is not an object")
        block = payload.get("quoteResponse")
        if not isinstance(block, dict):
            raise ProviderUnavailable(self.provider_id, "quote payload missing quoteResponse")
        rows = block.get("result")
        if not isinstance(rows, list):
            return {}
        mapped: dict[str, dict[str, Any]] = {}
        for row in rows:
            if isinstance(row, dict) and row.get("symbol"):
                mapped[str(row["symbol"]).upper()] = row
        return mapped

    async def batch_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Raw ``/v7/finance/quote`` rows, batched — shared with the OpenBB adapter.

        OpenBB has no batch quote route that survives 250+ symbols (its ``nasdaq``
        provider is one HTTP call per symbol), so the OpenBB adapter fills any
        gap with this feed and tags those quotes with ``source="yahoo"``.
        """
        rows: dict[str, dict[str, Any]] = {}
        for group in chunks(symbols, self._batch_size):
            rows.update(await self._quotes(group))
        return rows

    async def daily_closes(self, symbols: list[str]) -> dict[str, list[float]]:
        """Daily closes for the last month, batched — feeds the 7d/30d windows."""
        return await self._spark_batch(symbols, range_="1mo", interval="1d")

    async def intraday_closes(self, symbols: list[str]) -> dict[str, list[float]]:
        """5-minute closes for the current session — feeds the 1h window."""
        return await self._spark_batch(symbols, range_="1d", interval=INTRADAY_INTERVAL)

    async def _spark_batch(
        self, symbols: list[str], *, range_: str, interval: str
    ) -> dict[str, list[float]]:
        closes: dict[str, list[float]] = {}
        for group in chunks(symbols, SPARK_BATCH_SIZE):
            try:
                payload = await self._authorized(
                    "/v8/finance/spark",
                    {"symbols": ",".join(group), "range": range_, "interval": interval},
                )
            except ProviderUnavailable as exc:
                # Quotes stay usable without the series: affected windows degrade
                # to ``None`` instead of failing the whole snapshot.
                LOGGER.warning(
                    "yahoo spark %s/%s unavailable for %d symbols: %s",
                    range_,
                    interval,
                    len(group),
                    exc,
                )
                continue
            for symbol, closes_for_symbol in _spark_closes(payload).items():
                if closes_for_symbol:
                    closes[symbol] = closes_for_symbol
        return closes

    # ------------------------------------------------------------------ model

    def _to_quote(
        self,
        row: dict[str, Any] | None,
        closes: list[float] | None,
        intraday: list[float] | None,
        symbol: str,
        universe: Universe,
    ) -> Quote | None:
        if row is None:
            return None
        price = as_float(row.get("regularMarketPrice"))
        if price is None and closes:
            price = closes[-1]
        if price is None:
            return None

        changes: dict[ChangeWindow, float | None] = {
            ChangeWindow.H1: pct_change(intraday, INTRADAY_BARS_1H) if intraday else None,
            ChangeWindow.H24: as_float(row.get("regularMarketChangePercent")),
            ChangeWindow.D7: pct_change(closes, _WINDOW_SESSIONS[ChangeWindow.D7])
            if closes
            else None,
            ChangeWindow.D30: pct_change(closes, _WINDOW_SESSIONS[ChangeWindow.D30])
            if closes
            else None,
        }
        as_of = parse_epoch(row.get("regularMarketTime")) or self._now()
        quality = DataQuality.OK
        if self._now() - as_of > STALE_AFTER:
            quality = DataQuality.STALE
        else:
            # A window the universe advertises but we cannot compute is a partial
            # quote: the board would otherwise show a silent gap in a supported view.
            missing = [
                window
                for window in (ChangeWindow.H1, ChangeWindow.D7, ChangeWindow.D30)
                if window in universe.supported_windows and changes[window] is None
            ]
            if missing:
                quality = DataQuality.PARTIAL

        traded = as_float(row.get("regularMarketVolume"))
        volume = traded * price if traded is not None else None

        fallback_name = universe.name_of(symbol) or symbol
        name = str(row.get("shortName") or row.get("longName") or fallback_name)
        if self.asset_class is not AssetClass.US_EQUITY:
            name = fallback_name
        logo_url, logo_fallback = self._logos.urls(symbol) if self._logos else (None, None)

        return Quote(
            symbol=symbol,
            name=name,
            asset_class=universe.asset_class,
            price=price,
            currency=str(row.get("currency") or universe.currency),
            changes=changes,
            market_cap=as_float(row.get("marketCap")),
            volume=volume,
            sector=universe.sector_of(symbol),
            rank=None,
            external_id=symbol,
            logo_url=logo_url,
            logo_fallback=logo_fallback,
            source=self.provider_id,
            as_of=as_of,
            quality=quality,
        )

    def _cadence_warnings(self, universe: Universe, returned: int) -> list[ProviderWarning]:
        expected = len(universe.instruments)
        if expected and returned < expected:
            return [
                ProviderWarning(
                    provider=self.provider_id,
                    code="partial_coverage",
                    message=f"{returned}/{expected} symbols quoted",
                    severity="warning",
                )
            ]
        return []


def _first_result(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        return None
    result = chart.get("result")
    if isinstance(result, list) and result and isinstance(result[0], dict):
        return result[0]
    return None


def _spark_closes(payload: Any) -> dict[str, list[float]]:
    """Normalise every spark response shape into ``{SYMBOL: close series}``.

    Yahoo serves at least three shapes: a flat ``{SYMBOL: {close: [...]}}`` map,
    the same map wrapped in ``{"spark": {"result": [...]}}``, and a single
    ``{"symbol": ..., "close": [...]}`` object. Anything unrecognised is skipped
    so one odd block cannot poison the batch.
    """
    if not isinstance(payload, dict):
        return {}

    spark = payload.get("spark")
    if isinstance(spark, dict):
        return _spark_closes(spark.get("result", spark))

    single = payload.get("symbol")
    close = payload.get("close")
    if isinstance(single, str) and isinstance(close, list):
        values = _floats(close)
        return {single.upper(): values} if values else {}

    result = payload.get("result")
    if isinstance(result, list):
        collected: dict[str, list[float]] = {}
        for item in result:
            collected.update(_spark_closes(item))
        return collected

    mapped: dict[str, list[float]] = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            mapped.update(_spark_closes({**value, "symbol": value.get("symbol") or key}))
    return mapped


def _floats(raw: list[Any]) -> list[float]:
    return [value for value in (as_float(item) for item in raw) if value is not None]
