"""Concurrent build queue: success, failure, timeout isolation, fan-out."""

import threading
import time

from isg.queue import run_pages


def test_all_succeed_in_order():
    out = run_pages(["a", "b", "c"], lambda p: None, concurrency=2, timeout=2)
    assert out.success
    assert list(out.ok) == ["a", "b", "c"]


def test_failure_is_contained_and_reported():
    def render(p):
        if p == "b":
            raise RuntimeError("boom")

    out = run_pages(["a", "b", "c"], render, concurrency=2, timeout=2)
    assert list(out.ok) == ["a", "c"]
    assert out.failed == {"b": "boom"}
    assert not out.timed_out


def test_slow_page_does_not_block_unrelated_pages():
    def slow(p):
        if p == "a":
            time.sleep(1.0)

    t0 = time.monotonic()
    out = run_pages(["a", "b", "c"], slow, concurrency=2, timeout=0.2)
    elapsed = time.monotonic() - t0
    assert out.timed_out == ["a"]
    assert set(out.ok) == {"b", "c"}
    # The queue must not wait for the 1s slow render.
    assert elapsed < 0.8, f"queue stalled behind slow page for {elapsed:.2f}s"
    time.sleep(1.0)
    # Late completion of the detached render must not rewrite the outcome.
    assert out.timed_out == ["a"] and set(out.ok) == {"b", "c"}


def test_concurrency_cap_is_respected():
    active = 0
    peak = 0
    lock = threading.Lock()

    def render(_p):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    run_pages([str(i) for i in range(12)], render, concurrency=3, timeout=2)
    assert peak == 3


def test_duplicate_pages_rendered_once_with_deterministic_outcome():
    counter = {"n": 0}

    def render(_p):
        counter["n"] += 1

    out = run_pages(["x", "x", "x"], render, concurrency=2, timeout=1)
    assert counter["n"] == 1
    assert list(out.ok) == ["x"]


def test_invalid_arguments_rejected():
    import pytest

    with pytest.raises(ValueError):
        run_pages(["a"], lambda p: None, concurrency=0, timeout=1)
    with pytest.raises(ValueError):
        run_pages(["a"], lambda p: None, concurrency=1, timeout=0)
