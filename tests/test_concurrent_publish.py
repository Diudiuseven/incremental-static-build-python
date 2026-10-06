"""Readers never observe a mixed/half version while builds are publishing."""

from __future__ import annotations

import threading

from isg.manifest import read_release_manifest
from isg.model import ChangeEvent, ChangeKind
from isg.sources import route_output_relpath


def test_readers_see_only_complete_releases_during_churn(
        started_engine, site):
    engine = started_engine
    stop = threading.Event()
    violations: list[str] = []
    seen_releases: set[str] = set()

    def reader() -> None:
        while not stop.is_set():
            release_dir = engine.store.current_dir()
            if release_dir is None:
                violations.append("no current")
                continue
            # Resolve once per observation; everything must come from the
            # same release directory (no cross-release mixing).
            try:
                info = read_release_manifest(release_dir)
            except FileNotFoundError:
                # Manifest briefly swapped is impossible (atomic switch);
                # but tolerate the exact rename boundary by re-resolving.
                continue
            for route, (_digest, _obj) in info.pages.items():
                artifact = release_dir / route_output_relpath(route)
                if not artifact.exists():
                    violations.append(f"missing {route} in {info.release_id}")
                elif artifact.stat().st_size == 0:
                    violations.append(f"empty {route}")
            seen_releases.add(info.release_id)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for t in readers:
        t.start()

    def writer() -> None:
        for i in range(30):
            name = f"w{i % 6}.md"
            (site / "content" / name).write_text(
                f"---\ntitle: W{i % 6}\ndata: posts.json\n---\n# W\n\nv{i}")
            engine.submit_many([
                ChangeEvent(ChangeKind.UPSERT, f"content/{name}")])

    w = threading.Thread(target=writer)
    w.start()
    w.join()
    assert engine.wait_idle(15)
    stop.set()
    for t in readers:
        t.join()

    assert not violations, violations
    assert len(seen_releases) >= 2  # multiple versions were observed

    # Final state must equal a fresh full rebuild (incremental convergence).
    inc = {r: v[0] for r, v in engine.current().pages.items()}
    full = {r: v[0] for r, v in engine.full_rebuild().pages.items()}
    assert inc == full
