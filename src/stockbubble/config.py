"""Runtime configuration (env driven, ``BUBBLE_`` prefix)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def project_root() -> Path:
    """Repository root (``src/stockbubble/config.py`` -> ``parents[2]``)."""
    return Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Every knob is overridable via ``BUBBLE_*`` env vars or a local ``.env``."""

    model_config = SettingsConfigDict(
        env_prefix="BUBBLE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_title: str = "Stock Bubble"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    universes_dir: Path = Field(default_factory=lambda: project_root() / "config" / "universes")
    web_dir: Path = Field(default_factory=lambda: project_root() / "web")

    quote_ttl_seconds: float = Field(default=45.0, gt=0)
    series_ttl_seconds: float = Field(default=300.0, gt=0)
    http_timeout_seconds: float = Field(default=12.0, gt=0)
    http_retries: int = Field(default=2, ge=0, le=5)

    max_nodes: int = Field(default=400, ge=10, le=1000)
    auto_refresh_seconds: int = Field(default=60, ge=10, le=3600)
    color_clamp_pct: float = Field(default=10.0, gt=0)

    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    coingecko_api_key: SecretStr | None = None
    yahoo_base_url: str = "https://query1.finance.yahoo.com"

    #: US equity quote route: ``yahoo`` (batch, default) or ``openbb``.
    #:
    #: OpenBB is wired in because it is the top-starred finance project, but its
    #: free quote route is one HTTP request per symbol (~1s each measured on the
    #: nasdaq route, so ~4 分钟 for the 258-symbol board) and nasdaq's edge
    #: rejects aiohttp at the TLS layer — ``adapters.openbb_quote.patch_nasdaq_transport``
    #: swaps that transport for httpx so the route works at all. That is fine for
    #: a spot check, not for a 60s auto-refreshing board, so the batch Yahoo feed
    #: is the default and OpenBB is opt-in (``BUBBLE_US_PROVIDER=openbb``).
    us_provider: Literal["openbb", "yahoo"] = "yahoo"
    openbb_quote_provider: str = "nasdaq"
    openbb_chunk_size: int = Field(default=60, ge=10, le=200)
    openbb_concurrency: int = Field(default=4, ge=1, le=8)

    def coingecko_key(self) -> str | None:
        return self.coingecko_api_key.get_secret_value() if self.coingecko_api_key else None
