"""API routes. All handlers are thin: parse input, call one use case, map output."""

from __future__ import annotations

from dataclasses import dataclass
from os import getenv

from fastapi import APIRouter, Query

from stockbubble import __version__
from stockbubble.api.schemas import (
    ConfigDTO,
    HealthDTO,
    ProviderHealthDTO,
    SeriesDTO,
    SizeBasisOption,
    SnapshotDTO,
    UniverseDTO,
    WindowOption,
)
from stockbubble.application.services import AppServices
from stockbubble.domain.errors import UnsupportedSizeBasis, UnsupportedWindow
from stockbubble.domain.models import ChangeWindow, SizeBasis, Timeframe


@dataclass(frozen=True, slots=True)
class UiSettings:
    """Presentation knobs the frontend needs but the domain does not own."""

    title: str = "Stock Bubble"
    auto_refresh_seconds: int = 60
    color_clamp_pct: float = 10.0
    max_nodes: int = 400


def build_router(services: AppServices, ui: UiSettings) -> APIRouter:
    router = APIRouter(prefix="/api")
    revision = getenv("BUBBLE_BUILD_SHA") or getenv("VERCEL_GIT_COMMIT_SHA") or None

    @router.get("/config", response_model=ConfigDTO, summary="UI bootstrap config")
    async def get_config() -> ConfigDTO:
        return ConfigDTO(
            title=ui.title,
            version=__version__,
            revision=revision,
            windows=[WindowOption(value=w.value, label=w.label) for w in ChangeWindow],
            size_bases=[SizeBasisOption(value=b.value, label=b.label) for b in SizeBasis],
            auto_refresh_seconds=ui.auto_refresh_seconds,
            color_clamp_pct=ui.color_clamp_pct,
            max_nodes=ui.max_nodes,
        )

    @router.get("/universes", response_model=list[UniverseDTO], summary="Available universes")
    async def get_universes() -> list[UniverseDTO]:
        return [UniverseDTO.from_domain(universe) for universe in services.universes.all()]

    @router.get("/bubbles", response_model=SnapshotDTO, summary="Bubble snapshot")
    async def get_bubbles(
        universe: str | None = Query(default=None, description="universe id"),
        window: str | None = Query(default=None, description="1h | 24h | 7d | 30d"),
        size: str | None = Query(default=None, description="market_cap | volume"),
        limit: int | None = Query(default=None, ge=1, le=1000),
    ) -> SnapshotDTO:
        target = universe or services.universes.all()[0].id
        snapshot = await services.bubble.snapshot(
            target,
            window=_parse_window(window),
            size_basis=_parse_basis(size),
            limit=limit,
        )
        return SnapshotDTO.from_domain(snapshot)

    @router.get("/history", response_model=SeriesDTO, summary="Sparkline series")
    async def get_history(
        symbol: str = Query(min_length=1, max_length=24),
        universe: str | None = Query(default=None),
        timeframe: str = Query(default="1mo", description="1d | 5d | 1mo | 3mo | 1y"),
    ) -> SeriesDTO:
        target = universe or services.universes.all()[0].id
        series = await services.history.series(
            universe_id=target,
            symbol=symbol,
            timeframe=_parse_timeframe(timeframe),
        )
        return SeriesDTO.from_domain(series)

    @router.get("/health", response_model=HealthDTO, summary="Liveness + wiring")
    async def get_health() -> HealthDTO:
        return HealthDTO(
            status="ok",
            version=__version__,
            revision=revision,
            providers=[
                ProviderHealthDTO(
                    id=provider.provider_id,
                    asset_class=provider.asset_class.value,
                    windows=[
                        window.value
                        for window in ChangeWindow
                        if window in provider.supported_windows
                    ],
                )
                for provider in services.providers.all()
            ],
            universes=len(services.universes.all()),
        )

    return router


def _parse_window(raw: str | None) -> ChangeWindow | None:
    if not raw:
        return None
    try:
        return ChangeWindow.parse(raw)
    except ValueError as exc:
        raise UnsupportedWindow(str(exc)) from exc


def _parse_basis(raw: str | None) -> SizeBasis | None:
    if not raw:
        return None
    try:
        return SizeBasis(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in SizeBasis)
        msg = f"unknown size basis {raw!r}"
        raise UnsupportedSizeBasis(msg, hint=f"allowed: {allowed}") from exc


def _parse_timeframe(raw: str) -> Timeframe:
    try:
        return Timeframe(raw.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Timeframe)
        msg = f"unknown timeframe {raw!r}; allowed: {allowed}"
        raise ValueError(msg) from exc
