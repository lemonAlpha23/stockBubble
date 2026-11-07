"""Cached access to provider quotes, shared by every read use case."""

from __future__ import annotations

from dataclasses import dataclass

from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.models import ProviderWarning, Quote, Universe
from stockbubble.domain.ports import QuoteCache


@dataclass(frozen=True, slots=True)
class FeedResult:
    quotes: tuple[Quote, ...]
    warnings: tuple[ProviderWarning, ...]
    stale: bool
    age_seconds: float


class MarketFeed:
    """Thin application level facade over one provider + one cache entry."""

    def __init__(self, *, providers: ProviderRegistry, cache: QuoteCache) -> None:
        self._providers = providers
        self._cache = cache

    @staticmethod
    def cache_key(universe: Universe) -> str:
        return f"quotes:{universe.provider}:{universe.id}:{universe.currency}"

    async def quotes(self, universe: Universe) -> FeedResult:
        provider = self._providers.get(universe.provider)
        key = self.cache_key(universe)
        cached = await self._cache.load(key, lambda: provider.fetch_quotes(universe))
        result = cached.value
        warnings = list(result.warnings)
        if cached.stale:
            warnings.append(
                ProviderWarning(
                    provider=provider.provider_id,
                    code="stale_cache",
                    message="上游刷新失败，当前展示的是缓存快照",
                    severity="warning",
                )
            )
        return FeedResult(
            quotes=result.quotes,
            warnings=tuple(warnings),
            stale=cached.stale,
            age_seconds=cached.age_seconds,
        )

    async def invalidate(self, universe: Universe) -> None:
        key = self.cache_key(universe)
        invalidate = getattr(self._cache, "invalidate", None)
        if callable(invalidate):
            invalidate(key)
