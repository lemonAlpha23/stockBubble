from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from stockbubble.domain.models import (
    AssetClass,
    ChangeWindow,
    DataQuality,
    Instrument,
    Quote,
    SizeBasis,
    Universe,
)

NOW = datetime(2026, 11, 6, 2, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1h", ChangeWindow.H1),
        ("1H", ChangeWindow.H1),
        (" 24h ", ChangeWindow.H24),
        ("1d", ChangeWindow.H24),
        ("day", ChangeWindow.H24),
        ("1w", ChangeWindow.D7),
        ("week", ChangeWindow.D7),
        ("1m", ChangeWindow.D30),
        ("month", ChangeWindow.D30),
    ],
)
def test_parse_window_accepts_aliases(raw: str, expected: ChangeWindow) -> None:
    assert ChangeWindow.parse(raw) is expected


def test_parse_window_reports_allowed_values() -> None:
    with pytest.raises(ValueError, match="unsupported window"):
        ChangeWindow.parse("2y")


def test_window_labels_are_chinese() -> None:
    assert ChangeWindow.H24.label == "24 小时"
    assert SizeBasis.MARKET_CAP.label == "市值"
    assert AssetClass.US_EQUITY.label == "美股"


def test_universe_windows_use_canonical_order() -> None:
    universe = Universe(
        id="coins",
        name="币",
        asset_class=AssetClass.CRYPTO,
        provider="fake",
        supported_windows=(ChangeWindow.D30, ChangeWindow.H1),
        default_window=ChangeWindow.H1,
    )
    assert universe.windows == (ChangeWindow.H1, ChangeWindow.D30)
    assert universe.is_dynamic is True


def test_universe_rejects_default_window_outside_supported() -> None:
    with pytest.raises(ValidationError, match="default_window"):
        Universe(
            id="coins",
            name="币",
            asset_class=AssetClass.CRYPTO,
            provider="fake",
            supported_windows=(ChangeWindow.H24,),
            default_window=ChangeWindow.D7,
        )


def test_universe_rejects_duplicate_symbols() -> None:
    with pytest.raises(ValidationError, match="duplicate symbols"):
        Universe(
            id="equity",
            name="美股",
            asset_class=AssetClass.US_EQUITY,
            provider="fake",
            instruments=(
                Instrument(symbol="AAA", name="A"),
                Instrument(symbol="aaa", name="A again"),
            ),
        )


def test_universe_rejects_sectors_for_unknown_symbols() -> None:
    with pytest.raises(ValidationError, match="unknown symbols"):
        Universe(
            id="equity",
            name="美股",
            asset_class=AssetClass.US_EQUITY,
            provider="fake",
            instruments=(Instrument(symbol="AAA", name="A"),),
            sectors={"ZZZ": "科技"},
        )


def test_instrument_symbols_are_normalised() -> None:
    instrument = Instrument(symbol="  msft ", name="Microsoft")
    assert instrument.symbol == "MSFT"


def test_universe_sector_lookup_prefers_instrument_then_map() -> None:
    static = Universe(
        id="equity",
        name="美股",
        asset_class=AssetClass.US_EQUITY,
        provider="fake",
        instruments=(Instrument(symbol="AAA", name="A 公司", sector="科技"),),
    )
    assert static.sector_of("aaa") == "科技"
    assert static.name_of("AAa") == "A 公司"
    assert static.sector_of("NOPE") is None

    dynamic = Universe(
        id="coins",
        name="币",
        asset_class=AssetClass.CRYPTO,
        provider="fake",
        sectors={"BTC": "公链"},
    )
    assert dynamic.sector_of("btc") == "公链"


def test_quote_change_lookup_returns_none_for_missing_window() -> None:
    quote = Quote(
        symbol="AAA",
        name="A",
        asset_class=AssetClass.US_EQUITY,
        price=10.0,
        changes={ChangeWindow.H24: 1.5},
        source="fake",
        as_of=NOW,
    )
    assert quote.change(ChangeWindow.H24) == 1.5
    assert quote.change(ChangeWindow.D7) is None
    assert quote.quality is DataQuality.OK
