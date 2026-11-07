"""Provider registry — the single place that knows which adapters exist."""

from __future__ import annotations

from stockbubble.domain.errors import ProviderNotFound, UniverseConfigError
from stockbubble.domain.ports import MarketDataProvider


class ProviderRegistry:
    """Explicit, order preserving map of ``provider_id -> provider``."""

    def __init__(self) -> None:
        self._providers: dict[str, MarketDataProvider] = {}

    def register(self, provider: MarketDataProvider) -> ProviderRegistry:
        provider_id = provider.provider_id
        if provider_id in self._providers:
            msg = f"duplicate provider id {provider_id!r}"
            raise UniverseConfigError(msg)
        self._providers[provider_id] = provider
        return self

    def get(self, provider_id: str) -> MarketDataProvider:
        provider = self._providers.get(provider_id)
        if provider is None:
            known = ", ".join(sorted(self._providers)) or "<none>"
            msg = f"no provider registered as {provider_id!r}"
            raise ProviderNotFound(msg, hint=f"known: {known}")
        return provider

    def all(self) -> tuple[MarketDataProvider, ...]:
        return tuple(self._providers.values())

    def __contains__(self, provider_id: object) -> bool:
        return provider_id in self._providers

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
