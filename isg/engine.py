"""Build orchestration engine.

The engine wires together the source tree, dependency graph, build
executor and release store. It provides:

* **incremental builds** -- only pages invalidated by coalesced changes are
  rendered; repeated/reordered/burst edits converge on the full-rebuild
  result because every cycle renders from one fresh source snapshot;
* **full rebuild fallback** -- when a batch affects at least
  ``full_rebuild_threshold`` of the site the whole site is rebuilt, using
  the *same* render path, guaranteeing identical artifacts;
* **atomic publishing** -- releases are staged completely and ``current``
  is switched atomically; in-flight readers keep the old release;
* **failure containment** -- a page failure or timeout fails the cycle
  without touching the published release, and the next successful cycle
  retries it (no double application, no half-written directories);
* **deletion cleanup** -- removed pages and their index entries vanish
  from the new manifest; stale objects/old releases are pruned;
* **deterministic concurrent edits** -- all submissions are serialized
  through one coalescing queue and one build lock.
"""

from __future__ import annotations

import datetime as _dt
import itertools
import threading
import time
from pathlib import Path

from isg.config import Config
from isg.model import ChangeEvent, ReleaseInfo
from isg.planner import (DependencyGraph, Planner)
from isg.queue import BoundedExecutor, CoalescingQueue
from isg.release import ReleaseStore
from isg.render import RenderError, Renderer, markdown_to_html
from isg.sources import (SourceSnapshot, parse_frontmatter, read_source_tree,
                         route_output_relpath)


class BuildFailure(Exception):
    """Raised when one or more pages fail in a build cycle."""


class Engine:
    def __init__(self, source_root: str | Path,
                 output_root: str | Path,
                 config: Config | None = None) -> None:
        self.source_root = Path(source_root)
        self.output_root = Path(output_root)
        self.config = config or Config()
        self.config.validate()

        self.store = ReleaseStore(self.output_root, self.config.keep_releases)
        self.queue = CoalescingQueue(self.config.batch_size)
        self.executor = BoundedExecutor(self.config.concurrency)
        self.graph = DependencyGraph()
        self.planner = Planner(self.graph)

        self._lock = threading.RLock()       # serializes build cycles
        self._wake = threading.Condition()    # queue/idle notification
        self._counter = itertools.count(1)
        self._last_revision = 0
        self._active_cycles = 0
        self._cycle_done = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._index: dict[str, str] = {}     # route -> title, for index page

        self.store.recover()
        current = self.store.current()
        if current is not None:
            self.graph = DependencyGraph.from_manifest(current.deps)
            self.planner = Planner(self.graph)
            self._last_revision = current.plan_revision

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="isg-builder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        # Signal the builder first and wait for it to exit *before*
        # shutting down the pool it renders on. Ordering matters: joining
        # the pool while a cycle is in flight would deadlock, and stopping
        # the builder without joining leaves non-daemon workers that block
        # interpreter exit.
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join()
        # Wait for healthy workers, but never block past a grace period:
        # a timed-out non-cooperative render runs on a daemon worker and
        # cannot corrupt the (staging-discarded) state.
        self.executor.shutdown(
            wait=True, timeout=max(self.config.page_timeout * 2, 1.0))

    # -- editing API -----------------------------------------------------
    def submit(self, event: ChangeEvent) -> None:
        self.queue.put(event)
        with self._wake:
            self._wake.notify()

    def submit_many(self, events: list[ChangeEvent]) -> None:
        self.queue.put_many(events)
        with self._wake:
            self._wake.notify()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until all queued changes are published (or timeout)."""
        deadline = None if timeout is None else time.monotonic() + timeout

        def _is_busy() -> tuple[bool, int]:
            # Read both state pieces under the cycle condition's lock only;
            # ``_active_cycles`` is mutated while holding that same lock in
            # _build_cycle, so no nested _lock acquisition is needed (that
            # would invert lock ordering and deadlock).
            with self._cycle_done:
                return self._active_cycles > 0, self._active_cycles

        while True:
            building, _ = _is_busy()
            if not building and len(self.queue) == 0:
                return True
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
            with self._cycle_done:
                # Re-check inside the condition to avoid lost wakeups.
                if self._active_cycles == 0 and len(self.queue) == 0:
                    return True
                self._cycle_done.wait(timeout=remaining)

    def rebuild_now(self) -> ReleaseInfo:
        return self._build_cycle()

    def full_rebuild(self) -> ReleaseInfo:
        info = self._build_cycle(force_full=True)
        if info is None:  # empty source: return current or fail loudly
            raise BuildFailure("full rebuild produced no release")
        return info

    # -- reads -----------------------------------------------------------
    def current(self) -> ReleaseInfo | None:
        return self.store.current()

    def read_current_file(self, route: str) -> bytes | None:
        info = self.store.current()
        if info is None or route not in info.pages:
            return None
        _digest, object_rel = info.pages[route]
        path = self.store.object_path(object_rel)
        if not path.exists():
            # Hardlinks inside release dir are the durable copy.
            release_dir = self.store.current_dir()
            if release_dir is not None:
                candidate = release_dir / route_output_relpath(route)
                if candidate.exists():
                    return candidate.read_bytes()
            return None
        return path.read_bytes()

    # -- background loop -------------------------------------------------
    def _run_loop(self) -> None:
        idle = max(self.config.batch_idle_seconds, 0.001)
        while not self._stop.is_set():
            if not self.queue.wait_for_items(timeout=idle):
                continue
            # Coalesce bursts: let a flurry of edits settle briefly.
            time.sleep(idle)
            try:
                self._build_cycle()
            except Exception:
                # A failed cycle must not kill the builder; queued/newer
                # changes trigger a retry that converges to the snapshot.
                pass

    # -- snapshot --------------------------------------------------------
    def _snapshot(self) -> SourceSnapshot:
        return read_source_tree(self.source_root)

    # -- page rendering --------------------------------------------------
    def _load_json(self, snapshot: SourceSnapshot, name: str):
        import json
        rel = name if name.startswith("data/") else f"data/{name}"
        if rel not in snapshot.data:
            raise RenderError(f"missing data file: {rel}")
        return json.loads(snapshot.data[rel].decode("utf-8"))

    def _render_route(self, route: str, analysis, snapshot: SourceSnapshot,
                      renderer: Renderer) -> bytes:
        text = snapshot.contents[analysis.content_path].decode(
            "utf-8", "replace")
        meta, body = parse_frontmatter(text)
        title = str(meta.get("title", route))
        context = {
            "title": title,
            "site_name": str(meta.get("site_name", "Site")),
            "body_html": markdown_to_html(body),
            "route": route,
        }
        for name in analysis.data_files:
            key = name.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            context[key] = self._load_json(snapshot, name)
        html_text = renderer.render(analysis.template, context)
        return html_text.encode("utf-8")

    def _build_page(self, route: str, analyses: dict,
                    snapshot: SourceSnapshot) -> tuple[str, bytes, set]:
        """Render one page. Returns (route, artifact bytes, dependency keys)."""
        renderer = Renderer(snapshot.templates)
        analysis = analyses[route]
        data = self._render_route(route, analysis, snapshot, renderer)
        shared = renderer.partials()
        includes = renderer.discover_includes(analysis.template)
        deps = self.planner.discover_dependencies(
            analysis, includes, shared)
        return route, data, deps

    # -- build cycle -----------------------------------------------------
    def _build_cycle(self, force_full: bool = False) -> ReleaseInfo | None:
        with self._lock:
            with self._cycle_done:
                self._active_cycles += 1
            try:
                return self._build_cycle_locked(force_full)
            finally:
                with self._cycle_done:
                    self._active_cycles -= 1
                    self._cycle_done.notify_all()

    def _build_cycle_locked(self,
                            force_full: bool) -> ReleaseInfo | None:
        # Drain everything currently queued so a cycle processes the
        # latest coalesced change set against a fresh snapshot.
        events = self.queue.drain()
        snapshot = self._snapshot()
        current_info = self.store.current()
        total_pages = (len(current_info.pages) if current_info else 0)

        analyses = {a.route: a
                    for a in self.planner.analyze_snapshot(snapshot)}
        current_routes = set(analyses)

        if force_full or current_info is None:
            plan = self._full_plan(current_routes)
        else:
            if not events:
                return current_info
            plan = self.planner.plan(
                events, snapshot,
                total_pages=max(total_pages, 1),
                threshold=self.config.full_rebuild_threshold)
            # Safety reconciliation against the snapshot: a route present in
            # the published release but absent from the source tree must
            # always disappear, even if its delete notification was lost.
            # This guarantees the final tree cannot drift from a full rebuild
            # regardless of which events were delivered.
            vanished = set(current_info.pages) - current_routes
            if vanished:
                plan.routes_to_delete |= vanished
            # Re-evaluate the full-rebuild fallback after reconciliation.
            touched = len(plan.routes_to_build | plan.routes_to_delete)
            if touched and total_pages > 0 and \
                    touched / total_pages >= self.config.full_rebuild_threshold:
                plan = self._full_plan(current_routes)
                plan.routes_to_delete = set(self.graph.all_routes()) - \
                    current_routes
            # No route actually invalidated (e.g. edit to an unused file):
            # converge graph bookkeeping without publishing.
            if not plan.routes_to_build and not plan.routes_to_delete:
                return current_info

        if not current_routes and (current_info is None):
            return None

        release_id = self._new_release_id()
        staging = self.store.new_staging(release_id)
        try:
            info = self._materialize(
                release_id, staging, snapshot, analyses,
                current_info, plan)
        except BaseException:
            # Any failure (page error/timeout/crash): drop the staging
            # tree without moving ``current``. The published release stays
            # intact and a later cycle retries from the same snapshot.
            self.store.discard_staging(staging)
            raise

        # Publish only after the whole tree is staged.
        self.store.commit_staging(staging, info)
        self.graph = self._graph_from_info(info)
        self.planner = Planner(self.graph)
        self._last_revision = info.plan_revision
        self.store.prune()
        return info

    @staticmethod
    def _full_plan(routes: set):
        from isg.planner import Plan
        plan = Plan()
        plan.full_rebuild = True
        plan.routes_to_build = set(routes)
        return plan

    def _materialize(self, release_id: str, staging: Path,
                     snapshot: SourceSnapshot, analyses: dict,
                     base: ReleaseInfo | None, plan) -> ReleaseInfo:
        # Start from the previous release's routing table; incremental
        # cycles carry unchanged pages forward byte-for-byte.
        pages: dict[str, tuple[str, str]] = {}
        deps: dict[str, list[str]] = {}
        objects: dict[str, str] = {}
        if base is not None and not plan.full_rebuild:
            pages.update(base.pages)
            for route, keys in base.deps.items():
                if route not in plan.routes_to_delete:
                    deps[route] = list(keys)
            objects.update(base.objects)

        for gone in plan.routes_to_delete:
            pages.pop(gone, None)
            deps.pop(gone, None)
            self.graph.remove_route(gone)

        routes = sorted(plan.routes_to_build)
        results = self.executor.map(
            lambda r: self._build_page(r, analyses, snapshot),
            routes, timeout=self.config.page_timeout)

        failures: list[str] = []
        for route, (ok, value) in zip(routes, results):
            if not ok:
                failures.append(f"{route}: {value!r}")
                continue
            _route, data, page_deps = value
            object_rel = self.store.put_object(data)
            digest = object_rel.split("/")[-1]
            pages[route] = (digest, object_rel)
            objects[digest] = object_rel
            deps[route] = sorted(page_deps)

        if failures:
            # Do not publish a partial result: abort before commit. Keeping
            # previously-built pages only in staging is harmless because
            # staging is discarded by the caller.
            raise BuildFailure("; ".join(failures))

        # Every route in the snapshot must be present (full rebuild builds
        # all; incremental carried the rest forward).
        missing = set(analyses) - set(pages)
        if missing:
            raise BuildFailure(f"missing pages after build: {sorted(missing)}")

        # Keep the manifest self-contained: only objects referenced by this
        # release's pages belong in its object index.
        live_objects = {obj.split("/")[-1]: obj
                        for _d, obj in pages.values()}

        info = ReleaseInfo(
            release_id=release_id,
            created_at=_dt.datetime.now(_dt.timezone.utc)
            .isoformat(timespec="microseconds"),
            pages=dict(sorted(pages.items())),
            deps=dict(sorted(deps.items())),
            objects=dict(sorted(live_objects.items())),
            plan_revision=self._last_revision + 1,
        )
        return info

    def _new_release_id(self) -> str:
        return f"{int(time.time() * 1000):013d}-{next(self._counter):06d}"

    @staticmethod
    def _graph_from_info(info: ReleaseInfo) -> DependencyGraph:
        return DependencyGraph.from_manifest(info.deps)
