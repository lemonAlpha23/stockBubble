"""Ports (protocols) that the application layer depends on.

Everything here is structural: adapters do not inherit from these protocols,
they simply satisfy them. That keeps the dependency arrows pointing inwards.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from stockbubble.domain.models import (
    AssetClass,
    ChangeWindow,
    PricePoint,
    ProviderResult,
    Timeframe,
    Universe,
)


@dataclass(frozen=True, slots=True)
class Cached[T]:
    """A value plus how trustworthy its freshness is."""

    value: T
    stale: bool = False
    age_seconds: float = 0.0


@runtime_checkable
class MarketDataProvider(Protocol):
    """Reads live quotes for a universe from one upstream source."""

    provider_id: str
    asset_class: AssetClass
    supported_windows: frozenset[ChangeWindow]

    async def fetch_quotes(self, universe: Universe) -> ProviderResult:
        """Return quotes for ``universe``. Raises ``ProviderUnavailable``."""
        ...

    async def aclose(self) -> None:
        """Release transport resources."""
        ...


@runtime_checkable
class PriceSeriesProvider(Protocol):
    """Optional capability: historical closes for one instrument."""

    async def fetch_series(
        self,
        *,
        external_id: str,
        timeframe: Timeframe,
        currency: str,
    ) -> list[PricePoint]:
        """Return chronological closes. Raises ``ProviderUnavailable``."""
        ...


@runtime_checkable
class UniverseRepository(Protocol):
    """Read access to the configured universes."""

    def get(self, universe_id: str) -> Universe:
        """Return one universe or raise ``UniverseNotFound``."""
        ...

    def all(self) -> Sequence[Universe]:
        """Return every universe, in configuration order."""
        ...


@runtime_checkable
class QuoteCache(Protocol):
    """TTL cache with single-flight loading and stale-on-error fallback."""

    async def load[T](
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
    ) -> Cached[T]:
        """Return a cached value, refreshing it through ``loader`` when expired."""
        ...
