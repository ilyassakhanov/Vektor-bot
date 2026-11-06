"""Tests for the per-user RateLimiter (fake clock, no sleeping)."""

from __future__ import annotations

import threading

import pytest

from agent.rate_limit import RateDecision, RateLimiter


class FakeClock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _limiter(clock: FakeClock, max_per_window: int = 10) -> RateLimiter:
    return RateLimiter(max_per_window=max_per_window, window_s=60.0, clock=clock)


def test_first_acquire_ok() -> None:
    assert _limiter(FakeClock()).try_acquire("u") == RateDecision(ok=True, reason=None)


def test_second_acquire_while_in_flight_is_rejected_then_ok_after_release() -> None:
    lim = _limiter(FakeClock())
    assert lim.try_acquire("u").ok
    assert lim.try_acquire("u") == RateDecision(ok=False, reason="in_flight")
    lim.release("u")
    assert lim.try_acquire("u").ok


def test_window_limit_and_recovery() -> None:
    clock = FakeClock()
    lim = _limiter(clock)
    for _ in range(10):
        assert lim.try_acquire("u").ok
        lim.release("u")
        clock.now += 1.0
    assert lim.try_acquire("u") == RateDecision(ok=False, reason="window")
    # The first accepted acquisition was at t=1000; it leaves the window at 1060.
    clock.now = 1060.0
    assert lim.try_acquire("u").ok


def test_rejected_acquire_does_not_consume_a_slot() -> None:
    clock = FakeClock()
    lim = _limiter(clock, max_per_window=2)
    assert lim.try_acquire("u").ok
    for _ in range(5):
        assert lim.try_acquire("u").reason == "in_flight"
    lim.release("u")
    assert lim.try_acquire("u").ok  # second slot is still free
    lim.release("u")
    assert lim.try_acquire("u").reason == "window"


def test_in_flight_is_checked_before_the_window() -> None:
    lim = _limiter(FakeClock(), max_per_window=1)
    assert lim.try_acquire("u").ok
    assert lim.try_acquire("u").reason == "in_flight"


def test_users_are_isolated() -> None:
    lim = _limiter(FakeClock(), max_per_window=1)
    assert lim.try_acquire("a").ok
    assert lim.try_acquire("b").ok
    lim.release("a")
    assert lim.try_acquire("a").reason == "window"
    assert lim.try_acquire("b").reason == "in_flight"


def test_release_is_idempotent_and_safe_for_unknown_users() -> None:
    lim = _limiter(FakeClock())
    lim.release("nobody")
    assert lim.try_acquire("u").ok
    lim.release("u")
    lim.release("u")
    assert lim.try_acquire("u").ok


@pytest.mark.parametrize(
    ("max_per_window", "window_s"), [(0, 60.0), (-1, 60.0), (10, 0.0), (10, -5.0)]
)
def test_invalid_parameters_raise(max_per_window: int, window_s: float) -> None:
    with pytest.raises(ValueError):
        RateLimiter(max_per_window=max_per_window, window_s=window_s)


def test_concurrent_acquires_admit_exactly_one() -> None:
    lim = _limiter(FakeClock(), max_per_window=100)
    barrier = threading.Barrier(20)
    results: list[bool] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        ok = lim.try_acquire("u").ok
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
