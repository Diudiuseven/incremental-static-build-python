"""Atomic release switching, object store and recovery semantics."""

from __future__ import annotations

import datetime
import hashlib
from pathlib import Path

import pytest

from isg.release import ReleaseError, ReleaseStore
from isg.model import ReleaseInfo
from isg.sources import route_output_relpath


def make_info(release_id: str, routes: dict[str, bytes],
              revision: int = 1) -> ReleaseInfo:
    pages, objects = {}, {}
    for route, data in routes.items():
        digest = hashlib.sha256(data).hexdigest()
        obj = f"objects/{digest[:2]}/{digest}"
        pages[route] = (digest, obj)
        objects[digest] = obj
    return ReleaseInfo(
        release_id=release_id,
        created_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        pages=dict(sorted(pages.items())),
        deps={route: [] for route in pages},
        objects=objects,
        plan_revision=revision,
    )


def stage(store: ReleaseStore, info: ReleaseInfo,
          data_by_route: dict[str, bytes]) -> Path:
    for route, data in data_by_route.items():
        digest, obj = info.pages[route]
        placed = store.put_object(data)
        assert placed == obj
    return store.new_staging(info.release_id)


def test_commit_publishes_complete_tree(tmp_path):
    store = ReleaseStore(tmp_path / "out", keep_releases=1)
    data = {"/": b"<html>home</html>", "/a": b"<html>a</html>"}
    info = make_info("r1", data)
    staging = stage(store, info, data)
    store.commit_staging(staging, info)

    assert store.current().release_id == "r1"
    release_dir = store.current_dir()
    assert (release_dir / route_output_relpath("/")).read_bytes() == data["/"]
    assert (release_dir / "a.html").read_bytes() == data["/a"]


def test_failed_commit_never_moves_current(tmp_path):
    root = tmp_path / "out"
    store = ReleaseStore(root, keep_releases=1)
    data1 = {"/": b"v1"}
    info1 = make_info("r1", data1)
    store.commit_staging(stage(store, info1, data1), info1)

    # Reference a missing object: commit aborts before touching current.
    info2 = make_info("r2", {"/": b"v2"})
    info2.pages["/"] = (info2.pages["/"][0], "objects/de/admissing")
    staging2 = store.new_staging("r2")
    with pytest.raises(ReleaseError):
        store.commit_staging(staging2, info2)

    assert store.current().release_id == "r1"
    assert not (root / "releases" / "r2").exists()
    assert store.current_dir().joinpath("index.html").read_bytes() == b"v1"


def test_recover_cleans_interrupted_state(tmp_path):
    root = tmp_path / "out"
    store = ReleaseStore(root, keep_releases=1)
    data = {"/": b"home"}
    info = make_info("r1", data)
    store.commit_staging(stage(store, info, data), info)

    junk = store.staging_dir / "crashed"
    junk.mkdir(parents=True)
    (junk / "a.html").write_text("half")
    half = store.releases_dir / "partial"
    half.mkdir()
    (half / "index.html").write_text("half")

    fresh = ReleaseStore(root, keep_releases=1)
    fresh.recover()
    assert not junk.exists() and not half.exists()
    assert fresh.current().release_id == "r1"
    assert not any(fresh.staging_dir.iterdir())


def test_prune_drops_old_but_serves_current(tmp_path):
    root = tmp_path / "out"
    store = ReleaseStore(root, keep_releases=0)
    d1 = {"/": b"v1", "/a": b"a1"}
    i1 = make_info("r1", d1)
    store.commit_staging(stage(store, i1, d1), i1)

    d2 = {"/": b"v2", "/a": b"a1"}  # /a unchanged, dedups to same object
    i2 = make_info("r2", d2, revision=2)
    store.commit_staging(stage(store, i2, d2), i2)

    removed = store.prune()
    assert "r1" in removed
    assert store.current().release_id == "r2"
    cur = store.current_dir()
    assert (cur / "index.html").read_bytes() == b"v2"
    assert (cur / "a.html").read_bytes() == b"a1"


def test_objects_are_immutable_and_deduplicated(tmp_path):
    store = ReleaseStore(tmp_path / "out")
    a = store.put_object(b"same")
    b = store.put_object(b"same")
    assert a == b
    assert store.object_path(a).read_bytes() == b"same"
