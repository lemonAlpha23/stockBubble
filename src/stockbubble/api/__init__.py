"""HTTP transport layer: FastAPI app factory, DTOs and routes.

Nothing here owns business rules — handlers parse input, call one use case and
map the result into a DTO.
"""

from __future__ import annotations

from stockbubble.api.app import create_app
from stockbubble.api.routes import UiSettings, build_router
from stockbubble.api.schemas import (
    ConfigDTO,
    ErrorDTO,
    HealthDTO,
    NodeDTO,
    PointDTO,
    SeriesDTO,
    SnapshotDTO,
    UniverseDTO,
)

__all__ = [
    "ConfigDTO",
    "ErrorDTO",
    "HealthDTO",
    "NodeDTO",
    "PointDTO",
    "SeriesDTO",
    "SnapshotDTO",
    "UiSettings",
    "UniverseDTO",
    "build_router",
    "create_app",
]
