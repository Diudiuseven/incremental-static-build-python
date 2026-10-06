"""Concurrent page build queue with per-page timeouts and bounded fan-out.

Design goals
------------
* At most ``concurrency`` pages count as in-flight at the same time.
* A page whose render exceeds ``timeout`` is reported as timed out without
  blocking the queue: its logical slot is released immediately and unrelated
  pages keep being scheduled. The (possibly still running) render thread is
  detached and its late result is discarded via a compare-and-set.
* Every page appears in exactly one section of the returned BuildOutcome;
  outcomes are ordered by the input list, so identical runs are identical.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable

from .model import BuildOutcome

_RUNNING = "running"
_OK = "ok"
_FAILED = "failed"
_TIMED_OUT = "timed_out"
_TERMINAL = frozenset({_OK, _FAILED, _TIMED_OUT})


def run_pages(
    pages: list[str],
    render_one: Callable[[str], None],
    *,
    concurrency: int,
    timeout: float,
) -> BuildOutcome:
    """Run ``render_one(page)`` for each page with bounded concurrency.

    ``render_one`` should return on success and raise on failure.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if timeout <= 0:
        raise ValueError("timeout must be > 0")

    # A page listed more than once is still rendered once per outcome; the
    # last scheduled result wins consistently for every duplicate entry.
    unique_pages: list[str] = []
    seen: set[str] = set()
    for page in pages:
        if page not in seen:
            seen.add(page)
            unique_pages.append(page)

    state: dict[str, str] = {}
    errors: dict[str, str] = {}
    cond = threading.Condition()
    pending = deque(unique_pages)
    inflight: dict[str, float] = {}

    def worker(page: str) -> None:
        try:
            render_one(page)
            new_state, detail = _OK, ""
        except Exception as exc:  # noqa: BLE001 - failures stay inside the build
            new_state, detail = _FAILED, str(exc) or exc.__class__.__name__
        with cond:
            if state.get(page) == _RUNNING:
                state[page] = new_state
                if detail:
                    errors[page] = detail
                cond.notify_all()

    def launch(page: str) -> None:
        state[page] = _RUNNING
        inflight[page] = time.monotonic() + timeout
        threading.Thread(target=worker, args=(page,),
                         name=f"page-build:{page}", daemon=True).start()

    while pending or inflight:
        with cond:
            while pending and len(inflight) < concurrency:
                launch(pending.popleft())
            if inflight:
                wait_for = max(0.0, min(inflight.values()) - time.monotonic())
                cond.wait(timeout=wait_for)
                for page in [p for p, s in state.items()
                             if p in inflight and s in (_OK, _FAILED)]:
                    del inflight[page]
            now = time.monotonic()
            for page, deadline in list(inflight.items()):
                if state[page] == _RUNNING and now >= deadline:
                    state[page] = _TIMED_OUT
                    del inflight[page]

    ok: dict[str, str] = {}
    failed: dict[str, str] = {}
    timed_out: list[str] = []
    for page in unique_pages:
        status = state[page]
        if status == _OK:
            ok[page] = ""
        elif status == _FAILED:
            failed[page] = errors.get(page, "render error")
        else:
            timed_out.append(page)
    return BuildOutcome(ok=ok, failed=failed, timed_out=timed_out)
