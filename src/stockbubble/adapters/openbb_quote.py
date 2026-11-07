"""OpenBB Platform adapter — US equity quotes from the most-starred finance OSS.

`OpenBB <https://github.com/OpenBB-finance/OpenBB>`_ is the highest-starred
open-source finance project on GitHub (~75k stars at the time of writing) and
ships a provider-agnostic data platform. This adapter reads the quote snapshot
through OpenBB's ``equity.price.quote`` route (``nasdaq`` provider needs no key)
and derives the four change windows from closes:

* ``1h`` — 5-minute bars from the session so far;
* ``24h`` — OpenBB's own ``change_percent`` against the previous close;
* ``7d`` / ``30d`` — 5 / 21 daily sessions.

OpenBB has no batch daily-history route, so the multi-symbol series come from
the Yahoo spark endpoint the :class:`~stockbubble.adapters.yahoo.YahooProvider`
already speaks. Both are injected, so the adapter never opens a socket of its
own and stays testable with fakes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from stockbubble.adapters.logos import StockLogos
from stockbubble.adapters.support import as_float, chunks, downsample, pct_change
from stockbubble.adapters.yahoo import INTRADAY_BARS_1H, STALE_AFTER, YahooProvider
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

#: Which slice of the daily close series represents each window.
_WINDOW_SESSIONS: Mapping[ChangeWindow, int] = {
    ChangeWindow.D7: 5,
    ChangeWindow.D30: 21,
}

#: Daily history lookback per timeframe, in days.
_TIMEFRAME_LOOKBACK: Mapping[Timeframe, int] = {
    Timeframe.D5: 12,
    Timeframe.M1: 40,
    Timeframe.M3: 100,
    Timeframe.Y1: 380,
}

MAX_POINTS = 180

#: How long a failed quote route stays short-circuited, in seconds.
BREAKER_SECONDS = 300.0

#: Suffixes Nasdaq appends to instrument names.
_NAME_SUFFIXES = (" Common Stock", " Class A", " Class B", " Class C", " Ordinary Shares")


def _clean_name(raw: Any, fallback: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return fallback
    for suffix in _NAME_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text or fallback


#: Nasdaq's edge rejects ``aiohttp`` at the TLS/handshake layer: the same URL,
#: same headers and same IP answer 200 through httpx and 403 through aiohttp.
#: OpenBB's nasdaq provider talks aiohttp, so its quote route always came back
#: empty on this machine (and tripped the breaker). The shim below keeps
#: OpenBB's query/model/transform layer and swaps only the blocked transport.
NASDAQ_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)
_NASDAQ_TIMEOUT = 15.0


def patch_nasdaq_transport() -> bool:
    """Route ``openbb_nasdaq`` through httpx; return False when it is absent."""
    try:
        from openbb_nasdaq.utils import helpers
    except ImportError:  # pragma: no cover - provider missing
        return False
    if getattr(helpers.get_nasdaq_data, "_stockbubble_httpx", False):
        return True

    import httpx
    from openbb_core.app.model.abstract.error import OpenBBError

    headers = {
        "User-Agent": NASDAQ_USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }

    async def get_nasdaq_data(path: str, use_cache: bool = True, **kwargs: Any) -> Any:
        base = str(getattr(helpers, "BASE_URL", "https://api.nasdaq.com/api"))
        url = f"{base}/{path.lstrip('/')}"
        async with httpx.AsyncClient(timeout=_NASDAQ_TIMEOUT, headers=headers) as client:
            response = await client.get(url)
        if response.status_code != 200:
            raise OpenBBError(f"Nasdaq returned {response.status_code} for '{path}'.")
        payload = response.json()
        if not isinstance(payload, dict):
            raise OpenBBError(f"Unexpected Nasdaq response for '{path}'.")
        status = payload.get("status") or {}
        if status.get("rCode") not in (None, 200):
            raise OpenBBError(f"Nasdaq rejected '{path}': {status}")
        return payload.get("data")

    get_nasdaq_data._stockbubble_httpx = True  # type: ignore[attr-defined]
    helpers.get_nasdaq_data = get_nasdaq_data
    return True


def _rows_of(result: Any) -> list[dict[str, Any]]:
    """Normalise an ``OBBject`` into plain dicts without importing OpenBB."""
    rows: list[dict[str, Any]] = []
    for item in getattr(result, "results", ()) or ():
        if hasattr(item, "model_dump"):
            rows.append(dict(item.model_dump()))
        elif isinstance(item, Mapping):
            rows.append({str(key): value for key, value in item.items()})
    return rows


class OpenBBProvider:
    """US equity quotes via the OpenBB Platform."""

    provider_id = "openbb"
    asset_class = AssetClass.US_EQUITY
    supported_windows = frozenset(
        {ChangeWindow.H1, ChangeWindow.H24, ChangeWindow.D7, ChangeWindow.D30}
    )

    def __init__(
        self,
        *,
        series: YahooProvider,
        quote_provider: str = "nasdaq",
        chunk_size: int = 60,
        concurrency: int = 4,
        min_coverage: float = 0.6,
        logos: StockLogos | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._series = series
        self._logos = logos
        self._quote_provider = quote_provider
        self._chunk_size = max(1, chunk_size)
        self._concurrency = max(1, concurrency)
        self._min_coverage = min(max(min_coverage, 0.0), 1.0)
        self._now_fn = now or (lambda: datetime.now(tz=UTC))
        self._obb: Any = None
        self._down_until = 0.0
        # Importing the OpenBB platform costs ~15s the first time; pay it in the
        # background so the first page load does not.
        threading.Thread(target=self._warmup, name="openbb-warmup", daemon=True).start()

    def _warmup(self) -> None:
        try:
            self._platform()
        except ProviderUnavailable as exc:  # pragma: no cover - depends on install
            LOGGER.warning("openbb warmup skipped: %s", exc)
        except Exception as exc:  # pragma: no cover - import-time surprises
            LOGGER.warning("openbb warmup failed: %s", exc)

    # ------------------------------------------------------------------ api

    async def fetch_quotes(self, universe: Universe) -> ProviderResult:
        if universe.asset_class is not AssetClass.US_EQUITY:
            msg = f"openbb cannot serve asset_class {universe.asset_class.value!r}"
            raise UniverseConfigError(msg)
        symbols = list(universe.symbols())
        if not symbols:
            msg = f"universe {universe.id!r} has no static instruments for openbb"
            raise UniverseConfigError(msg)

        rows, errors, skipped = await self._quote_rows(symbols)
        openbb_symbols = set(rows)
        filled: list[str] = []
        coverage = len(openbb_symbols) / len(symbols)
        if coverage < self._min_coverage:
            # The OpenBB quote route is one request per symbol; behind a throttled
            # or filtered link it degrades to a handful of rows. Fill the board
            # from the batch feed instead of showing a near-empty page.
            missing = [symbol for symbol in symbols if symbol not in rows]
            try:
                fallback = await self._series.batch_quotes(missing)
            except ProviderUnavailable as exc:
                LOGGER.warning("openbb fallback feed failed: %s", exc)
                fallback = {}
            filled = sorted(fallback)
            rows.update(fallback)

        if not rows:
            detail = f"{type(errors[0]).__name__}: {errors[0]}" if errors else "no rows returned"
            raise ProviderUnavailable(
                self.provider_id, f"no quotes from openbb or the fallback feed ({detail})"
            )

        daily = await self._series.daily_closes(symbols)
        intraday = await self._series.intraday_closes(symbols)

        filled_set = set(filled)
        quotes: list[Quote] = []
        for symbol in symbols:
            source = self.provider_id if symbol not in filled_set else self._series.provider_id
            quote = self._to_quote(
                rows.get(symbol),
                daily.get(symbol),
                intraday.get(symbol),
                symbol,
                universe,
                source=source,
            )
            if quote is not None:
                quotes.append(quote)

        warnings: list[ProviderWarning] = []
        missing = [symbol for symbol in symbols if symbol not in rows]
        if missing:
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="missing_symbols",
                    message=f"no quote for {len(missing)} symbol(s): {', '.join(missing[:8])}",
                    severity="warning",
                )
            )
        for error in errors[:2]:
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="chunk_failed",
                    message=f"{type(error).__name__}: {error}"[:180],
                    severity="warning",
                )
            )
        if skipped:
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="openbb_skipped",
                    message=(
                        "openbb route is in cool-down after a failure; quotes came "
                        f"from {self._series.provider_id}"
                    ),
                    severity="info",
                )
            )
        if filled:
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="quotes_fell_back",
                    message=(
                        f"openbb returned {len(openbb_symbols)}/{len(symbols)} quotes; "
                        f"{len(filled)} filled from {self._series.provider_id}"
                    ),
                    severity="warning",
                )
            )
        if len(quotes) < len(symbols):
            warnings.append(
                ProviderWarning(
                    provider=self.provider_id,
                    code="partial_coverage",
                    message=f"{len(quotes)}/{len(symbols)} symbols quoted",
                    severity="warning",
                )
            )
        return ProviderResult(quotes=tuple(quotes), warnings=tuple(warnings))

    async def fetch_series(
        self,
        *,
        external_id: str,
        timeframe: Timeframe,
        currency: str,
    ) -> list[PricePoint]:
        del currency
        lookback = _TIMEFRAME_LOOKBACK.get(timeframe)
        if lookback is None:
            # Intraday sparklines are not a daily-history query: keep the session
            # bars from the chart feed instead of improvising a coarse series.
            return await self._series.fetch_series(
                external_id=external_id, timeframe=timeframe, currency="USD"
            )
        start = (self._now_fn() - timedelta(days=lookback)).date().isoformat()
        try:
            result = await asyncio.to_thread(self._historical, external_id, start, timeframe)
        except ProviderUnavailable as exc:
            LOGGER.warning("openbb history failed for %s: %s", external_id, exc)
            return await self._series.fetch_series(
                external_id=external_id, timeframe=timeframe, currency="USD"
            )
        points = _points(result, timeframe)
        if not points:
            return await self._series.fetch_series(
                external_id=external_id, timeframe=timeframe, currency="USD"
            )
        return downsample(points, MAX_POINTS)

    async def aclose(self) -> None:
        await self._series.aclose()

    # ------------------------------------------------------------------ obb

    def _platform(self) -> Any:
        if self._obb is None:
            patch_nasdaq_transport()
            try:
                from openbb import obb
            except ImportError as exc:
                raise ProviderUnavailable(
                    self.provider_id,
                    "OpenBB is not installed — run `uv sync` (openbb + openbb-equity)",
                ) from exc
            self._obb = obb
        return self._obb

    def _quote_chunk(self, symbols: Sequence[str]) -> dict[str, dict[str, Any]]:
        result = self._platform().equity.price.quote(list(symbols), provider=self._quote_provider)
        mapped: dict[str, dict[str, Any]] = {}
        for row in _rows_of(result):
            symbol = str(row.get("symbol") or "").upper()
            if symbol:
                mapped[symbol] = row
        return mapped

    async def _quote_rows(
        self, symbols: Sequence[str]
    ) -> tuple[dict[str, dict[str, Any]], list[BaseException], bool]:
        """Fetch quote rows sequentially, aborting on the first failure.

        One unreachable chunk means the route is blocked or throttled, and five
        more attempts would only add tens of seconds to first paint. The circuit
        breaker then short-circuits the next loads for :data:`BREAKER_SECONDS`.
        """
        if time.monotonic() < self._down_until:
            return {}, [], True

        rows: dict[str, dict[str, Any]] = {}
        errors: list[BaseException] = []
        for group in chunks(list(symbols), self._chunk_size):
            try:
                rows.update(await asyncio.to_thread(self._quote_chunk, group))
            except Exception as exc:
                errors.append(exc)
                self._down_until = time.monotonic() + BREAKER_SECONDS
                LOGGER.warning(
                    "openbb quote route failed (%s) — breaker for %ss",
                    exc,
                    int(BREAKER_SECONDS),
                )
                break
        return rows, errors, False

    def _historical(self, symbol: str, start: str, timeframe: Timeframe) -> Any:
        interval = "1wk" if timeframe is Timeframe.Y1 else "1d"
        return self._platform().equity.price.historical(
            symbol, provider=self._quote_provider, start_date=start, interval=interval
        )

    # ------------------------------------------------------------------ model

    def _to_quote(
        self,
        row: dict[str, Any] | None,
        closes: list[float] | None,
        intraday: list[float] | None,
        symbol: str,
        universe: Universe,
        *,
        source: str | None = None,
    ) -> Quote | None:
        if row is None:
            return None
        # OpenBB rows use ``last_price``; batch-feed rows carry Yahoo's field names.
        price = (
            as_float(row.get("last_price"))
            or as_float(row.get("regularMarketPrice"))
            or as_float(row.get("close"))
        )
        if price is None:
            return None
        prev_close = as_float(row.get("prev_close")) or as_float(
            row.get("regularMarketPreviousClose")
        )

        day_change = _percent(row.get("change_percent"))
        if day_change is None:
            day_change = as_float(row.get("regularMarketChangePercent"))
        if day_change is None and prev_close:
            day_change = (price - prev_close) / prev_close * 100.0

        changes: dict[ChangeWindow, float | None] = {
            ChangeWindow.H1: pct_change(intraday, INTRADAY_BARS_1H) if intraday else None,
            ChangeWindow.H24: day_change,
            ChangeWindow.D7: pct_change(closes, _WINDOW_SESSIONS[ChangeWindow.D7])
            if closes
            else None,
            ChangeWindow.D30: pct_change(closes, _WINDOW_SESSIONS[ChangeWindow.D30])
            if closes
            else None,
        }

        as_of = self._now_fn()
        quality = DataQuality.OK
        if self._now_fn() - as_of > STALE_AFTER:  # pragma: no cover - defensive
            quality = DataQuality.STALE
        else:
            missing = [
                window
                for window in (ChangeWindow.H1, ChangeWindow.D7, ChangeWindow.D30)
                if window in universe.supported_windows and changes[window] is None
            ]
            if missing:
                quality = DataQuality.PARTIAL

        traded = as_float(row.get("volume")) or as_float(row.get("regularMarketVolume"))
        logo_url, logo_fallback = self._logos.urls(symbol) if self._logos else (None, None)
        fallback_name = universe.name_of(symbol) or symbol
        return Quote(
            symbol=symbol,
            name=_clean_name(
                row.get("name") or row.get("shortName") or row.get("longName"), fallback_name
            ),
            asset_class=AssetClass.US_EQUITY,
            price=price,
            currency=str(row.get("currency") or universe.currency),
            changes=changes,
            market_cap=as_float(row.get("market_cap")) or as_float(row.get("marketCap")),
            volume=traded * price if traded is not None else None,
            sector=universe.sector_of(symbol)
            or _text(row.get("sector"))
            or _text(row.get("sectorDisp")),
            rank=None,
            external_id=symbol,
            logo_url=logo_url,
            logo_fallback=logo_fallback,
            source=source or self.provider_id,
            as_of=as_of,
            quality=quality,
        )


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _percent(value: Any) -> float | None:
    """OpenBB's ``change_percent`` is a fraction; the board wants percent."""
    number = as_float(value)
    if number is None:
        return None
    return number * 100.0 if abs(number) <= 1.5 else number


def _points(result: Any, timeframe: Timeframe) -> list[PricePoint]:
    points: list[PricePoint] = []
    for row in _rows_of(result):
        close = as_float(row.get("close"))
        raw_date = row.get("date")
        moment: datetime | None = None
        if isinstance(raw_date, datetime):
            moment = raw_date if raw_date.tzinfo else raw_date.replace(tzinfo=UTC)
        elif raw_date is not None:
            try:
                moment = datetime.fromisoformat(str(raw_date)).replace(tzinfo=UTC)
            except ValueError:
                moment = None
        if moment is not None and close is not None:
            points.append(PricePoint(t=moment, c=close))
    if timeframe is Timeframe.Y1 and len(points) > MAX_POINTS:
        return points
    return points


__all__ = ["OpenBBProvider"]
