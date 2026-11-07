"""In-process TTL cache with single-flight loading and stale-on-error fallback.

The cache is deliberately tiny: the board only ever holds a handful of entries
(one per universe), so a plain dict is enough and avoids another runtime
dependency. It is intentionally *heterogeneous* — quotes and price series share
the same implementation — so the generic parameter lives on :meth:`TTLCache.load`
instead of on the class.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, cast

from stockbubble.domain.ports import Cached


@dataclass(slots=True)
class _Entry:
    value: Any
    stored_at: float


class TTLCache:
    """``ttl_seconds`` freshness window, one in-flight load per key."""

    def __init__(
        self,
        ttl_seconds: float,
        *,
        max_entries: int = 32,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            msg = "ttl_seconds must be positive"
            raise ValueError(msg)
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._clock = clock
        self._entries: dict[str, _Entry] = {}
        self._inflight: dict[str, asyncio.Future[Any]] = {}
        self._lock = asyncio.Lock()

    async def load[T](
        self,
        key: str,
        loader: Callable[[], Awaitable[T]],
    ) -> Cached[T]:
        now = self._clock()
        fresh = self._fresh_entry(key, now)
        if fresh is not None:
            age = now - fresh.stored_at
            return Cached(value=cast("T", fresh.value), stale=False, age_seconds=age)

        async with self._lock:
            now = self._clock()
            fresh = self._fresh_entry(key, now)
            if fresh is not None:
                return Cached(
                    value=cast("T", fresh.value),
                    stale=False,
                    age_seconds=now - fresh.stored_at,
                )
            task = self._inflight.get(key)
            owner = task is None
            if task is None:
                task = asyncio.ensure_future(loader())
                task.set_name(f"cache-load:{key}")
                self._inflight[key] = task

        try:
            value: T = await task
        except BaseException:
            if owner:
                self._inflight.pop(key, None)
            stale = self._entries.get(key)
            if stale is not None:
                return Cached(
                    value=cast("T", stale.value),
                    stale=True,
                    age_seconds=max(0.0, self._clock() - stale.stored_at),
                )
            raise

        stored_at = self._clock()
        if owner:
            self._inflight.pop(key, None)
            self._entries[key] = _Entry(value=value, stored_at=stored_at)
            self._evict()
        return Cached(value=value, stale=False, age_seconds=0.0)

    def invalidate(self, key: str | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)

    def age(self, key: str) -> float | None:
        entry = self._entries.get(key)
        return None if entry is None else self._clock() - entry.stored_at

    def _fresh_entry(self, key: str, now: float) -> _Entry | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        return entry if now - entry.stored_at < self._ttl else None

    def _evict(self) -> None:
        while len(self._entries) > self._max_entries:
            oldest = next(iter(self._entries))
            self._entries.pop(oldest, None)
