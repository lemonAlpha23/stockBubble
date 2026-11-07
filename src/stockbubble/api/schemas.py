"""HTTP response contracts (DTOs).

The API never leaks domain objects directly: every response is mapped here so
that domain refactors cannot silently change the wire format.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from stockbubble.application.history import SeriesResult
from stockbubble.domain.models import Snapshot, Universe


class ConfigDTO(BaseModel):
    title: str
    windows: list[WindowOption]
    size_bases: list[SizeBasisOption]
    auto_refresh_seconds: int
    color_clamp_pct: float
    max_nodes: int


class WindowOption(BaseModel):
    value: str
    label: str


class SizeBasisOption(BaseModel):
    value: str
    label: str


class UniverseDTO(BaseModel):
    id: str
    name: str
    asset_class: str
    asset_class_label: str
    provider: str
    currency: str
    windows: list[str]
    default_window: str
    limit: int | None
    instrument_count: int

    @classmethod
    def from_domain(cls, universe: Universe) -> UniverseDTO:
        return cls(
            id=universe.id,
            name=universe.name,
            asset_class=universe.asset_class.value,
            asset_class_label=universe.asset_class.label,
            provider=universe.provider,
            currency=universe.currency,
            windows=[window.value for window in universe.windows],
            default_window=universe.default_window.value,
            limit=universe.limit,
            instrument_count=len(universe.instruments),
        )


class NodeDTO(BaseModel):
    symbol: str
    name: str
    asset_class: str
    sector: str | None
    price: float
    currency: str
    change_pct: float | None
    changes: dict[str, float | None]
    size_value: float
    share: float
    rank: int
    quality: str
    source: str
    logo: str | None = None
    logo_fallback: str | None = None


class WarningDTO(BaseModel):
    provider: str
    code: str
    message: str
    severity: str


class StatsDTO(BaseModel):
    provider_quotes: int
    returned: int
    skipped: int
    trimmed: int
    total_size: float
    coverage: float


class UniverseRefDTO(BaseModel):
    id: str
    name: str
    asset_class: str


class SnapshotDTO(BaseModel):
    model_config = ConfigDict(json_schema_extra={"description": "One bubble canvas render"})

    universe: UniverseRefDTO
    generated_at: datetime
    window: str
    size_basis: str
    currency: str
    total_size: float
    nodes: list[NodeDTO]
    warnings: list[WarningDTO]
    stats: StatsDTO

    @classmethod
    def from_domain(cls, snapshot: Snapshot) -> SnapshotDTO:
        return cls(
            universe=UniverseRefDTO(
                id=snapshot.universe_id,
                name=snapshot.universe_name,
                asset_class=snapshot.asset_class.value,
            ),
            generated_at=snapshot.generated_at,
            window=snapshot.window.value,
            size_basis=snapshot.size_basis.value,
            currency=snapshot.currency,
            total_size=snapshot.stats.total_size,
            nodes=[
                NodeDTO(
                    symbol=node.symbol,
                    name=node.name,
                    asset_class=node.asset_class.value,
                    sector=node.sector,
                    price=node.price,
                    currency=node.currency,
                    change_pct=node.change_pct,
                    changes={window.value: value for window, value in node.changes.items()},
                    size_value=node.size_value,
                    share=node.share,
                    rank=node.rank,
                    quality=node.quality.value,
                    source=node.source,
                    logo=node.logo_url,
            logo_fallback=node.logo_fallback,
                )
                for node in snapshot.nodes
            ],
            warnings=[
                WarningDTO(
                    provider=warning.provider,
                    code=warning.code,
                    message=warning.message,
                    severity=warning.severity,
                )
                for warning in snapshot.warnings
            ],
            stats=StatsDTO(
                provider_quotes=snapshot.stats.provider_quotes,
                returned=snapshot.stats.returned,
                skipped=snapshot.stats.skipped,
                trimmed=snapshot.stats.trimmed,
                total_size=snapshot.stats.total_size,
                coverage=snapshot.stats.coverage,
            ),
        )


class PointDTO(BaseModel):
    t: int
    c: float


class SeriesDTO(BaseModel):
    symbol: str
    timeframe: str
    source: str
    points: list[PointDTO]

    @classmethod
    def from_domain(cls, series: SeriesResult) -> SeriesDTO:
        return cls(
            symbol=series.symbol,
            timeframe=series.timeframe.value,
            source=series.source,
            points=[
                PointDTO(t=int(point.t.timestamp() * 1000), c=point.c) for point in series.points
            ],
        )


class HealthDTO(BaseModel):
    status: str
    version: str
    providers: list[ProviderHealthDTO]
    universes: int


class ProviderHealthDTO(BaseModel):
    id: str
    asset_class: str
    windows: list[str]


class ErrorBodyDTO(BaseModel):
    code: str
    message: str


class ErrorDTO(BaseModel):
    error: ErrorBodyDTO


__all__ = [
    "ConfigDTO",
    "ErrorBodyDTO",
    "ErrorDTO",
    "HealthDTO",
    "NodeDTO",
    "PointDTO",
    "ProviderHealthDTO",
    "SeriesDTO",
    "SizeBasisOption",
    "SnapshotDTO",
    "StatsDTO",
    "UniverseDTO",
    "UniverseRefDTO",
    "WarningDTO",
    "WindowOption",
]
