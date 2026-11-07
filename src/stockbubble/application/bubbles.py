"""Snapshot use case: turn cached quotes into one render payload."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from stockbubble.application.feed import MarketFeed
from stockbubble.application.metrics import build_nodes
from stockbubble.domain.errors import UnsupportedWindow
from stockbubble.domain.models import (
    ChangeWindow,
    ProviderWarning,
    SizeBasis,
    Snapshot,
    SnapshotStats,
    Universe,
)
from stockbubble.domain.ports import UniverseRepository


class BubbleService:
    """Builds the immutable snapshot consumed by the canvas."""

    def __init__(
        self,
        *,
        universes: UniverseRepository,
        feed: MarketFeed,
        max_nodes: int = 400,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._universes = universes
        self._feed = feed
        self._max_nodes = max(1, max_nodes)
        self._clock = clock or (lambda: datetime.now(tz=UTC))

    async def snapshot(
        self,
        universe_id: str,
        *,
        window: ChangeWindow | None = None,
        size_basis: SizeBasis | None = None,
        limit: int | None = None,
    ) -> Snapshot:
        universe = self._universes.get(universe_id)
        resolved_window = self._resolve_window(universe, window)
        resolved_basis = self._resolve_basis(size_basis)
        effective_limit = self._effective_limit(universe, limit)

        feed = await self._feed.quotes(universe)
        build = build_nodes(
            feed.quotes,
            window=resolved_window,
            basis=resolved_basis,
            limit=effective_limit,
        )

        warnings = list(feed.warnings)
        if not build.nodes:
            warnings.append(
                ProviderWarning(
                    provider=universe.provider,
                    code="no_data",
                    message="当前口径下没有可展示的标的（检查 size basis 或上游数据）",
                    severity="error",
                )
            )

        provider_quotes = len(feed.quotes)
        returned = len(build.nodes)
        stats = SnapshotStats(
            provider_quotes=provider_quotes,
            returned=returned,
            skipped=build.skipped,
            trimmed=build.trimmed,
            total_size=build.total_size,
            coverage=(returned / provider_quotes) if provider_quotes else 0.0,
        )
        return Snapshot(
            universe_id=universe.id,
            universe_name=universe.name,
            asset_class=universe.asset_class,
            generated_at=self._clock(),
            window=resolved_window,
            size_basis=resolved_basis,
            currency=universe.currency,
            nodes=build.nodes,
            warnings=tuple(warnings),
            stats=stats,
        )

    @staticmethod
    def _resolve_window(universe: Universe, window: ChangeWindow | None) -> ChangeWindow:
        resolved = window or universe.default_window
        if resolved not in universe.windows:
            allowed = ", ".join(item.value for item in universe.windows)
            raise UnsupportedWindow(
                f"universe {universe.id!r} does not support window {resolved.value!r}",
                hint=f"allowed: {allowed}",
            )
        return resolved

    @staticmethod
    def _resolve_basis(size_basis: SizeBasis | None) -> SizeBasis:
        return size_basis or SizeBasis.VOLUME

    def _effective_limit(self, universe: Universe, limit: int | None) -> int:
        requested = limit or universe.limit or self._max_nodes
        return max(1, min(requested, self._max_nodes))
