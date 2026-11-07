from __future__ import annotations

from datetime import UTC, datetime

import pytest

from stockbubble.adapters.support import (
    as_float,
    chunks,
    downsample,
    parse_epoch,
    parse_iso,
    pct_change,
)


def test_chunks_splits_and_keeps_remainder() -> None:
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunks([], 3)) == []
    with pytest.raises(ValueError, match="chunk size"):
        list(chunks([1], 0))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (True, None),
        (7, 7.0),
        (2.5, 2.5),
        ("1,234.5", 1234.5),
        (" 12 ", 12.0),
        ("n/a", None),
        (float("nan"), None),
        (float("inf"), None),
        ([], None),
    ],
)
def test_as_float_is_defensive(raw: object, expected: float | None) -> None:
    assert as_float(raw) == expected


def test_parse_iso_normalises_to_utc() -> None:
    assert parse_iso("2026-11-06T10:00:00+08:00") == datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    assert parse_iso("2026-11-06T02:00:00Z") == datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    assert parse_iso("2026-11-06T02:00:00") == datetime(2026, 11, 6, 2, 0, tzinfo=UTC)
    assert parse_iso("") is None
    assert parse_iso("yesterday") is None
    assert parse_iso(12) is None


def test_parse_epoch_rejects_non_positive() -> None:
    assert parse_epoch(0) is None
    assert parse_epoch(-5) is None
    assert parse_epoch("1762394400") == datetime(2025, 11, 6, 2, 0, tzinfo=UTC)


def test_pct_change_uses_sessions_back() -> None:
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 110.0]
    assert pct_change(closes, 1) == pytest.approx((110.0 / 104.0 - 1) * 100)
    assert pct_change(closes, 5) == pytest.approx(10.0)
    assert pct_change(closes, 0) is None
    assert pct_change(closes, 6) is None
    assert pct_change([0.0, 10.0], 1) is None


def test_downsample_keeps_endpoints_and_target_size() -> None:
    points = list(range(100))
    reduced = downsample(points, 10)
    assert len(reduced) == 10
    assert reduced[0] == 0
    assert reduced[-1] == 99
    assert downsample(points, 1) == points
    assert downsample([1, 2, 3], 5) == [1, 2, 3]
