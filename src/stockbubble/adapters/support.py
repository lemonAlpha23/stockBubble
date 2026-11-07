"""Small parsing helpers shared by HTTP adapters."""

from __future__ import annotations

import math
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from typing import Any


def chunks[T](items: Iterable[T], size: int) -> Iterator[list[T]]:
    """Yield consecutive slices of at most ``size`` items."""
    if size < 1:
        msg = "chunk size must be >= 1"
        raise ValueError(msg)
    bucket: list[T] = []
    for item in items:
        bucket.append(item)
        if len(bucket) == size:
            yield bucket
            bucket = []
    if bucket:
        yield bucket


def as_float(value: Any) -> float | None:
    """Best effort numeric coercion; ``None`` for anything unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.replace(",", "").strip())
        except ValueError:
            return None
    else:
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_epoch(value: Any) -> datetime | None:
    number = as_float(value)
    if number is None or number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def pct_change(closes: list[float], sessions_back: int) -> float | None:
    """Percent change between the last close and ``sessions_back`` closes ago."""
    if sessions_back < 1 or len(closes) <= sessions_back:
        return None
    previous = closes[-1 - sessions_back]
    if previous <= 0:
        return None
    return (closes[-1] / previous - 1.0) * 100.0


def downsample[T](points: list[T], target: int) -> list[T]:
    """Evenly reduce ``points`` to at most ``target`` entries, keeping the last."""
    if target < 2 or len(points) <= target:
        return points
    step = (len(points) - 1) / (target - 1)
    picked = [points[round(i * step)] for i in range(target)]
    picked[-1] = points[-1]
    return picked
