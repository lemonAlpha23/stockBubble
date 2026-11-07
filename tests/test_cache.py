from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import pytest

from stockbubble.adapters.cache import TTLCache


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


async def test_load_caches_until_ttl_expires(clock: Clock) -> None:
    cache = TTLCache(10.0, clock=clock)
    calls: list[int] = []

    async def loader() -> str:
        calls.append(1)
        return "quote"

    first = await cache.load("k", loader)
    assert first.value == "quote"
    assert first.stale is False
    assert first.age_seconds == 0.0

    clock.now += 4.0
    second = await cache.load("k", loader)
    assert second.value == "quote"
    assert second.age_seconds == pytest.approx(4.0)
    assert len(calls) == 1

    clock.now += 7.0
    await cache.load("k", loader)
    assert len(calls) == 2


async def test_concurrent_loads_collapse_into_one_call(clock: Clock) -> None:
    cache = TTLCache(10.0, clock=clock)
    calls: list[int] = []
    release = asyncio.Event()

    async def loader() -> str:
        calls.append(1)
        await release.wait()
        return "value"

    pending = [asyncio.create_task(cache.load("k", loader)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*pending)
    assert [item.value for item in results] == ["value"] * 5
    assert len(calls) == 1


async def test_failure_falls_back_to_stale_value(clock: Clock) -> None:
    cache = TTLCache(5.0, clock=clock)
    await cache.load("k", _constant("fresh"))
    clock.now += 30.0

    async def broken() -> str:
        msg = "upstream down"
        raise RuntimeError(msg)

    cached = await cache.load("k", broken)
    assert cached.value == "fresh"
    assert cached.stale is True
    assert cached.age_seconds == pytest.approx(30.0)


async def test_cancelled_request_does_not_cancel_shared_loader(clock: Clock) -> None:
    cache = TTLCache(60.0, clock=clock)
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def loader() -> str:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return "shared"

    first = asyncio.create_task(cache.load("k", loader))
    await entered.wait()
    second = asyncio.create_task(cache.load("k", loader))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert (await second).value == "shared"
    assert (await cache.load("k", loader)).value == "shared"
    assert calls == 1


async def test_load_finishes_and_caches_after_sole_waiter_cancels(clock: Clock) -> None:
    cache = TTLCache(60.0, clock=clock)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def loader() -> str:
        entered.set()
        await release.wait()
        return "finished"

    request = asyncio.create_task(cache.load("k", loader))
    await entered.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    release.set()
    # Yield for the loader and its completion callback.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert cache.age("k") is not None
    assert (await cache.load("k", _constant("unexpected reload"))).value == "finished"


async def test_failure_without_previous_value_propagates(clock: Clock) -> None:
    cache = TTLCache(5.0, clock=clock)

    async def broken() -> str:
        msg = "upstream down"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="upstream down"):
        await cache.load("missing", broken)
    assert cache.age("missing") is None


async def test_invalidate_forces_reload(clock: Clock) -> None:
    cache = TTLCache(60.0, clock=clock)
    await cache.load("k", _constant("one"))
    cache.invalidate("k")
    assert cache.age("k") is None
    assert (await cache.load("k", _constant("two"))).value == "two"

    cache.invalidate()
    assert cache.age("k") is None


async def test_eviction_keeps_entries_bounded(clock: Clock) -> None:
    cache = TTLCache(60.0, max_entries=2, clock=clock)
    for key in ("a", "b", "c"):
        await cache.load(key, _constant(key))
    assert cache.age("a") is None
    assert cache.age("c") is not None


def test_rejects_non_positive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        TTLCache(0)


def _constant(value: str) -> Callable[[], Awaitable[str]]:
    async def loader() -> str:
        return value

    return loader
