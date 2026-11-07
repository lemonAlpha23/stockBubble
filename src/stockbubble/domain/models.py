"""Pure domain models.

This module is the innermost layer: it must not import anything from
``adapters``, ``application`` or ``api`` (enforced by import-linter and by
``tests/test_architecture.py``). Only pydantic is used, for validation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

USD = "USD"


class AssetClass(StrEnum):
    """Top-level asset family a ``Universe`` belongs to."""

    CRYPTO = "crypto"
    US_EQUITY = "us_equity"

    @property
    def label(self) -> str:
        return {AssetClass.CRYPTO: "加密货币", AssetClass.US_EQUITY: "美股"}[self]


class ChangeWindow(StrEnum):
    """Supported look-back windows for the color dimension."""

    H1 = "1h"
    H24 = "24h"
    D7 = "7d"
    D30 = "30d"

    @property
    def label(self) -> str:
        return {
            ChangeWindow.H1: "1 小时",
            ChangeWindow.H24: "24 小时",
            ChangeWindow.D7: "7 天",
            ChangeWindow.D30: "30 天",
        }[self]

    @property
    def lookback(self) -> timedelta:
        return {
            ChangeWindow.H1: timedelta(hours=1),
            ChangeWindow.H24: timedelta(hours=24),
            ChangeWindow.D7: timedelta(days=7),
            ChangeWindow.D30: timedelta(days=30),
        }[self]

    @classmethod
    def parse(cls, value: str) -> ChangeWindow:
        """Parse a user supplied window, accepting a few common aliases."""
        aliases = {
            "1h": "1h",
            "1hr": "1h",
            "hour": "1h",
            "24h": "24h",
            "1d": "24h",
            "day": "24h",
            "7d": "7d",
            "1w": "7d",
            "week": "7d",
            "30d": "30d",
            "1m": "30d",
            "month": "30d",
        }
        key = value.strip().lower()
        try:
            return cls(aliases.get(key, key))
        except ValueError as exc:
            allowed = ", ".join(item.value for item in cls)
            msg = f"unsupported window {value!r}; allowed: {allowed}"
            raise ValueError(msg) from exc


class SizeBasis(StrEnum):
    """Which number drives the bubble *area*."""

    MARKET_CAP = "market_cap"
    VOLUME = "volume"

    @property
    def label(self) -> str:
        return {SizeBasis.MARKET_CAP: "市值", SizeBasis.VOLUME: "24h 成交额"}[self]


class DataQuality(StrEnum):
    """Per-quote trust marker surfaced to the UI."""

    OK = "ok"
    PARTIAL = "partial"
    STALE = "stale"


class Timeframe(StrEnum):
    """Look-back span for the detail sparkline."""

    D1 = "1d"
    D5 = "5d"
    M1 = "1mo"
    M3 = "3mo"
    Y1 = "1y"

    @property
    def label(self) -> str:
        return {
            Timeframe.D1: "1 天",
            Timeframe.D5: "5 天",
            Timeframe.M1: "1 月",
            Timeframe.M3: "3 月",
            Timeframe.Y1: "1 年",
        }[self]


class Instrument(BaseModel):
    """A single tradable line inside a static universe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str = Field(min_length=1, max_length=24)
    name: str = Field(min_length=1, max_length=120)
    sector: str | None = Field(default=None, max_length=64)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            msg = "symbol must not be blank"
            raise ValueError(msg)
        return normalized


class Universe(BaseModel):
    """A named, validated basket of instruments served by one provider.

    A universe with no ``instruments`` is *dynamic*: the provider decides which
    lines to return (e.g. "top N coins by market cap").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    name: str = Field(min_length=1, max_length=80)
    asset_class: AssetClass
    provider: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,47}$")
    currency: str = Field(default=USD, min_length=3, max_length=3)
    default_window: ChangeWindow = ChangeWindow.H24
    supported_windows: tuple[ChangeWindow, ...] = (
        ChangeWindow.H24,
        ChangeWindow.D7,
        ChangeWindow.D30,
    )
    limit: int | None = Field(default=None, ge=1, le=1000)
    instruments: tuple[Instrument, ...] = ()
    sectors: dict[str, str] = Field(default_factory=dict)
    fallback_sector: str | None = Field(default=None, max_length=64)
    exclude: tuple[str, ...] = ()

    @property
    def is_dynamic(self) -> bool:
        return not self.instruments

    @property
    def windows(self) -> tuple[ChangeWindow, ...]:
        """Supported windows in canonical order, regardless of YAML ordering."""
        return tuple(w for w in ChangeWindow if w in self.supported_windows)

    def symbols(self) -> tuple[str, ...]:
        return tuple(item.symbol for item in self.instruments)

    def sector_of(self, symbol: str) -> str | None:
        key = symbol.strip().upper()
        for item in self.instruments:
            if item.symbol == key:
                return item.sector
        return self.sectors.get(key) or self.fallback_sector

    def name_of(self, symbol: str) -> str | None:
        key = symbol.strip().upper()
        for item in self.instruments:
            if item.symbol == key:
                return item.name
        return None

    @model_validator(mode="after")
    def _check_consistency(self) -> Universe:
        if not self.supported_windows:
            msg = f"universe {self.id!r}: supported_windows must not be empty"
            raise ValueError(msg)
        if self.default_window not in self.supported_windows:
            msg = (
                f"universe {self.id!r}: default_window {self.default_window.value!r} "
                "is not in supported_windows"
            )
            raise ValueError(msg)
        if self.instruments:
            symbols = [item.symbol for item in self.instruments]
            duplicates = sorted({s for s in symbols if symbols.count(s) > 1})
            if duplicates:
                msg = f"universe {self.id!r}: duplicate symbols {duplicates}"
                raise ValueError(msg)
            unknown = sorted(set(self.sectors) - set(symbols))
            if unknown:
                msg = f"universe {self.id!r}: sectors reference unknown symbols {unknown}"
                raise ValueError(msg)
        return self


class Quote(BaseModel):
    """One normalised, provider independent quote."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    asset_class: AssetClass
    price: float = Field(ge=0)
    currency: str = USD
    changes: dict[ChangeWindow, float | None] = Field(default_factory=dict)
    market_cap: float | None = Field(default=None, ge=0)
    volume: float | None = Field(default=None, ge=0)
    sector: str | None = None
    rank: int | None = Field(default=None, ge=1)
    external_id: str | None = None
    logo_url: str | None = None
    logo_fallback: str | None = None
    source: str
    as_of: datetime
    quality: DataQuality = DataQuality.OK

    def change(self, window: ChangeWindow) -> float | None:
        return self.changes.get(window)


class PricePoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    t: datetime
    c: float = Field(ge=0)


class ProviderWarning(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    code: str
    message: str
    severity: str = "warning"


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """What every ``MarketDataProvider`` hands back to the application layer."""

    quotes: tuple[Quote, ...]
    warnings: tuple[ProviderWarning, ...] = ()


class BubbleNode(BaseModel):
    """A single bubble, already sized and ranked by the application layer."""

    model_config = ConfigDict(frozen=True)

    symbol: str
    name: str
    asset_class: AssetClass
    sector: str | None
    price: float
    currency: str
    change_pct: float | None
    changes: dict[ChangeWindow, float | None]
    size_value: float
    share: float = Field(ge=0, le=1)
    rank: int = Field(ge=1)
    quality: DataQuality
    source: str
    logo_url: str | None = None
    logo_fallback: str | None = None


class SnapshotStats(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider_quotes: int = Field(ge=0)
    returned: int = Field(ge=0)
    skipped: int = Field(ge=0)
    trimmed: int = Field(ge=0)
    total_size: float = Field(ge=0)
    coverage: float = Field(ge=0, le=1)


class Snapshot(BaseModel):
    """Everything the bubble canvas needs for one render."""

    model_config = ConfigDict(frozen=True)

    universe_id: str
    universe_name: str
    asset_class: AssetClass
    generated_at: datetime
    window: ChangeWindow
    size_basis: SizeBasis
    currency: str
    nodes: tuple[BubbleNode, ...]
    warnings: tuple[ProviderWarning, ...] = ()
    stats: SnapshotStats
