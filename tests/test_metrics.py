from __future__ import annotations

import pytest
from tests.conftest import make_quote

from stockbubble.application.metrics import build_nodes, size_of
from stockbubble.domain.models import ChangeWindow, DataQuality, SizeBasis


def test_size_of_picks_requested_basis() -> None:
    quote = make_quote("AAA", market_cap=500.0, volume=7.0)
    assert size_of(quote, SizeBasis.MARKET_CAP) == 500.0
    assert size_of(quote, SizeBasis.VOLUME) == 7.0
    assert size_of(make_quote("AAA", market_cap=None), SizeBasis.MARKET_CAP) is None
    assert size_of(make_quote("AAA", volume=0.0), SizeBasis.VOLUME) is None


def test_build_nodes_ranks_by_market_cap_and_attaches_shares() -> None:
    quotes = [
        make_quote("AAA", market_cap=100.0),
        make_quote("BBB", market_cap=300.0),
        make_quote("CCC", market_cap=600.0),
    ]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP)
    assert [node.symbol for node in build.nodes] == ["CCC", "BBB", "AAA"]
    assert [node.rank for node in build.nodes] == [1, 2, 3]
    assert build.total_size == 1_000.0
    assert build.nodes[0].share == pytest.approx(0.6)
    assert sum(node.share for node in build.nodes) == pytest.approx(1.0)


def test_build_nodes_carries_logo_url() -> None:
    quotes = [make_quote("AAA", market_cap=10.0, logo_url="https://cdn.example/aaa.png")]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP)
    assert build.nodes[0].logo_url == "https://cdn.example/aaa.png"


def test_build_nodes_skips_unusable_sizes() -> None:
    quotes = [
        make_quote("AAA", market_cap=100.0),
        make_quote("BBB", market_cap=None),
        make_quote("CCC", market_cap=0.0),
    ]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP)
    assert [node.symbol for node in build.nodes] == ["AAA"]
    assert build.skipped == 2
    assert build.trimmed == 0


def test_build_nodes_honours_limit() -> None:
    quotes = [make_quote(name, market_cap=float(index + 1)) for index, name in enumerate("ABC")]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP, limit=2)
    assert [node.symbol for node in build.nodes] == ["C", "B"]
    assert build.trimmed == 1
    assert build.total_size == 5.0


def test_missing_change_marks_quote_partial() -> None:
    quotes = [
        make_quote("AAA", changes={ChangeWindow.H24: 2.0}),
        make_quote("BBB", changes={ChangeWindow.H24: None}),
        make_quote("CCC", changes={ChangeWindow.H24: None}, quality=DataQuality.STALE),
    ]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP)
    by_symbol = {node.symbol: node for node in build.nodes}
    assert by_symbol["AAA"].quality is DataQuality.OK
    assert by_symbol["AAA"].change_pct == 2.0
    assert by_symbol["BBB"].quality is DataQuality.PARTIAL
    assert by_symbol["BBB"].change_pct is None
    assert by_symbol["CCC"].quality is DataQuality.STALE


def test_build_nodes_without_quotes_is_empty() -> None:
    build = build_nodes([], window=ChangeWindow.D7, basis=SizeBasis.VOLUME)
    assert build.nodes == ()
    assert build.total_size == 0.0


def test_volume_basis_selects_same_top_market_caps() -> None:
    quotes = [
        make_quote("AAA", market_cap=300.0, volume=10.0),
        make_quote("BBB", market_cap=200.0, volume=20.0),
        make_quote("CCC", market_cap=100.0, volume=900.0),
        make_quote("DDD", market_cap=None, volume=1000.0),
    ]
    cap = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.MARKET_CAP, limit=2)
    volume = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.VOLUME, limit=2)
    assert [n.symbol for n in cap.nodes] == [n.symbol for n in volume.nodes] == ["AAA", "BBB"]
    assert [n.rank for n in volume.nodes] == [1, 2]
    assert [n.size_value for n in volume.nodes] == [10.0, 20.0]
    assert volume.nodes[0].share == pytest.approx(1 / 3)
    assert volume.total_size == 30.0
    assert volume.skipped == 1
    assert volume.trimmed == 1


def test_missing_volume_does_not_replace_a_top_market_cap() -> None:
    quotes = [
        make_quote("AAA", market_cap=300.0, volume=None),
        make_quote("BBB", market_cap=200.0, volume=20.0),
    ]
    build = build_nodes(quotes, window=ChangeWindow.H24, basis=SizeBasis.VOLUME, limit=1)
    assert [n.symbol for n in build.nodes] == ["AAA"]
    assert build.nodes[0].size_value == 0.0
    assert build.nodes[0].quality is DataQuality.PARTIAL
    assert build.nodes[0].share == 0.0
