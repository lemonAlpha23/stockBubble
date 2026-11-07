"""Application services container handed to the API layer."""

from __future__ import annotations

from dataclasses import dataclass

from stockbubble.application.bubbles import BubbleService
from stockbubble.application.feed import MarketFeed
from stockbubble.application.history import HistoryService
from stockbubble.application.registry import ProviderRegistry
from stockbubble.domain.ports import UniverseRepository


@dataclass(frozen=True, slots=True)
class AppServices:
    universes: UniverseRepository
    providers: ProviderRegistry
    feed: MarketFeed
    bubble: BubbleService
    history: HistoryService

    async def aclose(self) -> None:
        await self.providers.aclose()
