"""Bounded executor: per-task timeout and slow-task isolation."""

from __future__ import annotations

import time

from isg.queue import BoundedExecutor


def test_other_items_finish_when_one_is_slow():
    pool = BoundedExecutor(concurrency=4)
    try:
        def fn(x):
            if x == "slow":
                time.sleep(5.0)  # exceeds timeout; must not block others
            return f"ok:{x}"

        start = time.monotonic()
        results = pool.map(fn, ["a", "slow", "b"], timeout=0.3)
        elapsed = time.monotonic() - start

        assert results[0] == (True, "ok:a")
        assert results[1][0] is False  # slow item reported as failure
        assert results[2] == (True, "ok:b")
        assert elapsed < 1.5
    finally:
        pool.shutdown(timeout=0.1)


def test_page_exception_is_contained():
    pool = BoundedExecutor(concurrency=2)
    try:
        def fn(x):
            if x == "boom":
                raise ValueError("render failed")
            return x

        results = pool.map(fn, ["good", "boom", "fine"], timeout=1.0)
        assert results[0] == (True, "good")
        assert results[1][0] is False
        assert isinstance(results[1][1], ValueError)
        assert results[2] == (True, "fine")
    finally:
        pool.shutdown(timeout=0.5)
