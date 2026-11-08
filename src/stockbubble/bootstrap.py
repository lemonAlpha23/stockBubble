"""Composition root: the only module allowed to know every concrete adapter.

Wiring rules enforced here (fail fast, before the server starts):

1. every universe's ``provider`` must be registered;
2. its ``asset_class`` must match the provider's;
3. its ``supported_windows`` must be a subset of the provider's capability;
4. a static universe must list instruments, a dynamic one must not.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from stockbubble.adapters.cache import TTLCache
from stockbubble.adapters.coingecko import CoinGeckoProvider
from stockbubble.adapters.logos import StockLogos
from stockbubble.adapters.openbb_quote import OpenBBProvider
from stockbubble.adapters.universes import YamlUniverseRepository
from stockbubble.adapters.yahoo import YahooProvider
from stockbubble.api.routes import UiSettings
from stockbubble.application.bubbles import BubbleService
from stockbubble.application.feed import MarketFeed
from stockbubble.application.history import HistoryService
from stockbubble.application.registry import ProviderRegistry
from stockbubble.application.services import AppServices
from stockbubble.config import Settings
from stockbubble.domain.errors import ProviderUnavailable, UniverseConfigError
from stockbubble.domain.models import AssetClass, BubbleNode, ProviderWarning, Universe
from stockbubble.domain.ports import MarketDataProvider, QuoteCache

logger = logging.getLogger(__name__)


class _ProviderAlias:
    """Point a universe at another registered provider id, without editing YAML.

    ``config/universes/us_equity.yaml`` names the batch feed (``yahoo``) because
    that is the default quote path. ``BUBBLE_US_PROVIDER=openbb`` registers the
    OpenBB adapter under ``openbb`` and this wrapper rewrites the universe id at
    load time, so the YAML keeps telling the truth for the default route.
    """

    def __init__(self, inner: Any, mapping: dict[str, str]) -> None:
        self._inner = inner
        self._mapping = mapping

    def get(self, universe_id: str) -> Any:
        universe = self._inner.get(universe_id)
        provider = self._mapping.get(universe_id)
        if provider is None:
            return universe
        return universe.model_copy(update={"provider": provider})

    def all(self) -> list[Any]:
        return [self.get(universe.id) for universe in self._inner.all()]


def build_providers(settings: Settings) -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(
        CoinGeckoProvider(
            base_url=settings.coingecko_base_url,
            api_key=settings.coingecko_key(),
            timeout=settings.http_timeout_seconds,
            retries=settings.http_retries,
        )
    )
    yahoo = YahooProvider(
        base_url=settings.yahoo_base_url,
        timeout=settings.http_timeout_seconds,
        retries=settings.http_retries,
        logos=StockLogos(),
    )
    if settings.us_provider == "openbb":
        try:
            registry.register(
                OpenBBProvider(
                    series=yahoo,
                    logos=StockLogos(),
                    quote_provider=settings.openbb_quote_provider,
                    chunk_size=settings.openbb_chunk_size,
                    concurrency=settings.openbb_concurrency,
                )
            )
        except ProviderUnavailable as exc:  # pragma: no cover - depends on install
            logger.warning("openbb unavailable (%s) — falling back to yahoo quotes", exc)
            registry.register(yahoo)
    else:
        registry.register(yahoo)
    for asset_class in (AssetClass.HK_EQUITY, AssetClass.A_EQUITY):
        registry.register(
            YahooProvider(
                provider_id=f"yahoo_{asset_class.value}",
                asset_class=asset_class,
                logos=StockLogos(),
                base_url=settings.yahoo_base_url,
                timeout=settings.http_timeout_seconds,
                retries=settings.http_retries,
            )
        )
    return registry


def validate_universes(universes: Iterable[Universe], registry: ProviderRegistry) -> None:
    for universe in universes:
        try:
            provider = registry.get(universe.provider)
        except Exception as exc:
            msg = f"universe {universe.id!r} references unknown provider {universe.provider!r}"
            raise UniverseConfigError(msg, hint="register it in bootstrap.build_providers") from exc

        if provider.asset_class is not universe.asset_class:
            msg = (
                f"universe {universe.id!r}: asset_class {universe.asset_class.value!r} "
                f"does not match provider {provider.provider_id!r} "
                f"({provider.asset_class.value!r})"
            )
            raise UniverseConfigError(msg)

        missing = [w.value for w in universe.windows if w not in provider.supported_windows]
        if missing:
            msg = (
                f"universe {universe.id!r}: windows {missing} are not supported by "
                f"provider {provider.provider_id!r} "
                f"({sorted(w.value for w in provider.supported_windows)})"
            )
            raise UniverseConfigError(msg)

        if universe.is_dynamic and universe.exclude and universe.instruments:
            msg = f"universe {universe.id!r}: exclude only applies to dynamic universes"
            raise UniverseConfigError(msg)

        if not universe.is_dynamic and provider.provider_id == "yahoo" and not universe.instruments:
            msg = f"universe {universe.id!r}: yahoo requires an explicit instrument list"
            raise UniverseConfigError(msg)


def ui_settings(settings: Settings) -> UiSettings:
    return UiSettings(
        title=settings.app_title,
        auto_refresh_seconds=settings.auto_refresh_seconds,
        color_clamp_pct=settings.color_clamp_pct,
        max_nodes=settings.max_nodes,
    )


def build_services(settings: Settings) -> AppServices:
    universes: Any = YamlUniverseRepository.from_directory(settings.universes_dir)
    if settings.us_provider == "openbb":
        universes = _ProviderAlias(universes, {"us_equity": "openbb"})
    registry = build_providers(settings)
    validate_universes(universes.all(), registry)

    quote_cache: QuoteCache = TTLCache(settings.quote_ttl_seconds, max_entries=16)
    series_cache: QuoteCache = TTLCache(settings.series_ttl_seconds, max_entries=256)

    feed = MarketFeed(providers=registry, cache=quote_cache)

    for universe in universes.all():
        logger.info(
            "universe %-10s provider=%-9s class=%-9s window=%-4s size=%s",
            universe.id,
            universe.provider,
            universe.asset_class.value,
            universe.default_window.value,
            "dynamic" if universe.is_dynamic else f"{len(universe.instruments)} symbols",
        )

    return AppServices(
        universes=universes,
        providers=registry,
        feed=feed,
        bubble=BubbleService(universes=universes, feed=feed, max_nodes=settings.max_nodes),
        history=HistoryService(
            universes=universes,
            feed=feed,
            providers=registry,
            cache=series_cache,
        ),
    )


def provider_of(registry: ProviderRegistry, universe: Universe) -> MarketDataProvider:
    return registry.get(universe.provider)


def format_table(nodes: Iterable[BubbleNode], warnings: Iterable[ProviderWarning]) -> str:
    """Plain-text table used by ``stockbubble snapshot`` and by tests."""
    rows = ["  #  symbol      change      size        share  name"]
    for node in nodes:
        change = "   n/a" if node.change_pct is None else f"{node.change_pct:+7.2f}%"
        rows.append(
            f"{node.rank:>3}  {node.symbol:<10}  {change}  {node.size_value:>14,.0f}  "
            f"{node.share * 100:5.2f}%  {node.name}"
        )
    for warning in warnings:
        rows.append(
            f"! {warning.severity}: [{warning.provider}] {warning.code} — {warning.message}"
        )
    return "\n".join(rows)


__all__ = [
    "build_providers",
    "build_services",
    "format_table",
    "provider_of",
    "ui_settings",
    "validate_universes",
]
