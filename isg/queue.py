"""Coalescing change queue and a bounded, timeout-aware build executor."""

from __future__ import annotations

import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor, wait
from typing import Any, Callable

from isg.model import ChangeEvent


class _DaemonThreadPool(ThreadPoolExecutor):
    """ThreadPoolExecutor whose workers are daemon threads.

    A non-cooperative, timed-out render (Python cannot forcibly kill a
    thread) therefore never blocks interpreter or test shutdown. Render
    tasks hold no locks needed for consistency; failed cycles discard
    their staging tree independently.
    """

    def _adjust_thread_count(self) -> None:
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads < self._max_workers:
            thread_name = "%s_%d" % (self._thread_name_prefix or self,
                                     num_threads)
            from concurrent.futures.thread import _worker
            t = threading.Thread(
                name=thread_name, target=_worker,
                args=(weakref.ref(self, weakref_cb),
                      self._create_worker_context(),
                      self._work_queue),
                daemon=True)
            t.start()
            self._threads.add(t)
            from concurrent.futures import thread as _thread_mod
            _thread_mod._threads_queues[t] = self._work_queue


class CoalescingQueue:
    """Collects change events for source paths.

    Semantics:

    * **de-duplication / repetition:** repeated events for the same path
      coalesce; only the latest state matters.
    * **order independence:** the drained batch is sorted by path, so the
      build result never depends on arrival order; correctness is anchored
      to the source snapshot, not to event order.
    * **batching:** at most ``batch_size`` events are returned per drain.
    """

    def __init__(self, batch_size: int) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, ChangeEvent] = {}
        self._batch_size = max(1, batch_size)
        self._not_empty = threading.Condition(self._lock)

    def put(self, event: ChangeEvent) -> None:
        with self._not_empty:
            self._events[event.path] = event
            self._not_empty.notify()

    def put_many(self, events: list[ChangeEvent]) -> None:
        with self._not_empty:
            for event in events:
                self._events[event.path] = event
            if events:
                self._not_empty.notify_all()

    def drain(self, max_items: int | None = None) -> list[ChangeEvent]:
        limit = min(self._batch_size,
                    max_items if max_items is not None else self._batch_size)
        with self._not_empty:
            paths = sorted(self._events)[:limit]
            return [self._events.pop(p) for p in paths]

    def wait_for_items(self, timeout: float) -> bool:
        """Block until at least one event is queued (False on timeout)."""
        with self._not_empty:
            if self._events:
                return True
            return self._not_empty.wait(timeout)

    def snapshot_paths(self) -> list[str]:
        with self._lock:
            return sorted(self._events)

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


class BoundedExecutor:
    """Thread pool with independent per-task deadlines.

    Every item gets its own deadline measured from submission. Results are
    collected in completion order, so a slow page occupies only one worker
    slot and the other pages finish immediately rather than queueing behind
    it. A timed-out page is returned as a failure; Python cannot forcibly
    kill the worker thread, but cooperative builds keep it isolated.
    """

    def __init__(self, concurrency: int) -> None:
        self._concurrency = max(1, concurrency)
        self._pool = _DaemonThreadPool(
            max_workers=self._concurrency,
            thread_name_prefix="isg-build")

    def map(self, fn: Callable[[Any], Any], items: list,
            timeout: float) -> list[tuple[bool, Any]]:
        if not items:
            return []
        now = time.monotonic()
        pending: dict[Any, tuple[Any, float]] = {}
        for item in items:
            future = self._pool.submit(fn, item)
            pending[future] = (item, now + timeout)

        results: dict[int, tuple[bool, Any]] = {}
        while pending:
            # Wait only until the soonest outstanding deadline.
            wait_timeout = max(0.0, min(deadline - time.monotonic()
                                        for _item, deadline
                                        in pending.values()))
            done, _not_done = wait(
                set(pending), timeout=wait_timeout,
                return_when="FIRST_COMPLETED")
            for future in done:
                item, _deadline = pending.pop(future)
                try:
                    results[id(item)] = (True, future.result())
                except Exception as exc:  # page failures are contained
                    results[id(item)] = (False, exc)
            # Enforce per-item deadlines for anything still outstanding.
            for future in list(pending):
                item, deadline = pending[future]
                if time.monotonic() >= deadline:
                    pending.pop(future)
                    future.cancel()
                    results[id(item)] = (
                        False, TimeoutError(f"page build timed out: {item}"))

        return [results[id(item)] for item in items]

    def shutdown(self, wait: bool = True, timeout: float | None = None) -> None:
        """Shutdown the pool.

        With ``timeout``, workers are awaited only for that long; daemon
        workers cannot block process exit, so a still-running non-
        cooperative render is abandoned rather than stalling shutdown.
        """
        if not wait:
            self._pool.shutdown(wait=False)
            return
        if timeout is None:
            self._pool.shutdown(wait=True)
            return
        # ThreadPoolExecutor.shutdown has no timeout; join daemon threads
        # ourselves with a deadline and leave stragglers behind.
        self._pool.shutdown(wait=False)
        deadline = time.monotonic() + timeout
        for thread in list(self._pool._threads):  # type: ignore[attr-defined]
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(timeout=remaining)
