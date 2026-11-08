"""Logo resolver tests: deterministic URLs, verified coverage, graceful gaps."""

from __future__ import annotations

import pytest

from stockbubble.adapters.logos import FIRST_SOURCE, LogoSources, StockLogos


@pytest.fixture(scope="module")
def logos() -> StockLogos:
    return StockLogos()


def test_primary_is_parqet_by_default(logos: StockLogos) -> None:
    primary, fallback = logos.urls("AAPL")

    assert primary == "https://assets.parqet.com/logos/symbol/AAPL?format=png"
    assert fallback == "https://companiesmarketcap.com/img/company-logos/64/AAPL.png"


@pytest.mark.parametrize("symbol", ["0700.HK", "600519.SS", "000858.SZ"])
def test_regional_listings_have_primary_and_fallback(symbol: str, logos: StockLogos) -> None:
    primary, fallback = logos.urls(symbol)
    assert primary == f"https://financialmodelingprep.com/image-stock/{symbol}.png"
    assert fallback == f"https://assets.parqet.com/logos/symbol/{symbol}?format=png"


def test_symbol_is_uppercased_and_stripped(logos: StockLogos) -> None:
    assert logos.urls("  aapl ") == logos.urls("AAPL")


def test_share_class_dashes_survive(logos: StockLogos) -> None:
    primary, _ = logos.urls("BRK-B")

    assert primary.endswith("/BRK-B?format=png")


@pytest.mark.parametrize("symbol", sorted(FIRST_SOURCE))
def test_known_gaps_start_on_the_other_source(symbol: str, logos: StockLogos) -> None:
    primary, fallback = logos.urls(symbol)

    assert "companiesmarketcap.com" in primary
    assert fallback is not None and "parqet" in fallback


def test_unsafe_symbol_yields_no_url(logos: StockLogos) -> None:
    assert logos.urls("../../etc/passwd") == ("", None)
    assert logos.urls("   ") == ("", None)


def test_custom_order_and_sources() -> None:
    custom = StockLogos(
        order=("companiesmarketcap",),
        first_source={},
        sources=LogoSources(parqet="https://p/{symbol}", companiesmarketcap="https://c/{symbol}"),
    )
    primary, fallback = custom.urls("MSFT")

    assert primary == "https://c/MSFT"
    assert fallback is None


def test_unknown_source_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown logo source"):
        StockLogos(order=("nope",))
