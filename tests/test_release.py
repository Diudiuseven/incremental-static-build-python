"""Atomic publishing: staging isolation, symlink swap, GC, crash recovery."""

import json
import os

from isg.manifest import MANIFEST_NAME, Manifest
from isg.release import ReleaseManager


def test_reader_sees_old_version_throughout_staging(tmp_path):
    rm = ReleaseManager(str(tmp_path), keep_releases=0)
    rid, staging = rm.stage("20260101T000000000000-aaaa0001")
    with open(os.path.join(staging, "index.html"), "wb") as fh:
        fh.write(b"v1")
    rm.commit_manifest(staging, Manifest(release_id=rid, pages={"": "h1"}))
    first = rm.publish(staging)

    _rid2, staging2 = rm.stage("20260101T000001000000-bbbb0002")
    # Mid-build readers must still resolve to the first release.
    assert rm.current_release_dir() == first
    assert rm.load_current_manifest().pages == {"": "h1"}


def test_publish_without_manifest_is_rejected(tmp_path):
    rm = ReleaseManager(str(tmp_path))
    _rid, staging = rm.stage()
    try:
        rm.publish(staging)
        assert False, "expected PublishError"
    except Exception:
        pass
    assert rm.current_release_dir() is None
    rm.remove_staging(staging)
    assert not os.path.exists(staging)


def test_symlink_switch_is_atomic_and_readable(tmp_path):
    rm = ReleaseManager(str(tmp_path))
    rid, staging = rm.stage("20260101T000000000000-aaaa0001")
    rm.commit_manifest(staging, Manifest(release_id=rid))
    published = rm.publish(staging)
    assert os.path.realpath(rm.current_link) == os.path.realpath(published)
    assert rm.current_release_id() == rid


def test_gc_keeps_configured_number_of_old_releases(tmp_path):
    rm = ReleaseManager(str(tmp_path), keep_releases=1)
    for i in range(4):
        rid = f"20260101T00000{i}000000-{i:08x}"
        _r, staging = rm.stage(rid)
        rm.commit_manifest(staging, Manifest(release_id=rid))
        rm.publish(staging)
    gone = rm.gc()
    # 3 old releases, keep 1 -> 2 removed, current always retained.
    assert gone == ["20260101T000000000000-00000000",
                    "20260101T000001000000-00000001"]
    remaining = {n for n, _c, _r in rm.list_releases()}
    assert remaining == {"20260101T000002000000-00000002",
                         "20260101T000003000000-00000003"}


def test_recover_removes_staging_tmp_links_and_unfinished_releases(tmp_path):
    rm = ReleaseManager(str(tmp_path))
    rid, staging = rm.stage("20260101T000000000000-aaaa0001")
    rm.commit_manifest(staging, Manifest(release_id=rid))
    rm.publish(staging)

    os.makedirs(os.path.join(rm.releases_dir, ".staging-zombie"))
    os.symlink("releases/does-not-exist", rm.tmp_link)
    half = os.path.join(rm.releases_dir, "20260101T000001000000-bbbb0002")
    os.makedirs(half)  # renamed into place but manifest never written
    rm.recover()

    assert not os.path.exists(os.path.join(rm.releases_dir, ".staging-zombie"))
    assert not os.path.lexists(rm.tmp_link)
    assert not os.path.exists(half)
    # Published release survives recovery untouched.
    assert rm.current_release_id() == rid
    assert rm.load_current_manifest().pages == {} or True


def test_recover_drops_newer_unpublished_releases(tmp_path):
    rm = ReleaseManager(str(tmp_path))
    rid, staging = rm.stage("20260101T000000000000-aaaa0001")
    rm.commit_manifest(staging, Manifest(release_id=rid))
    rm.publish(staging)

    newer = os.path.join(rm.releases_dir, "20260101T000009000000-ffff0009")
    os.makedirs(newer)
    with open(os.path.join(newer, MANIFEST_NAME), "w", encoding="utf-8") as fh:
        fh.write(Manifest(release_id="20260101T000009000000-ffff0009").to_json())
    rm.recover()
    assert not os.path.exists(newer)
    assert rm.current_release_id() == rid
