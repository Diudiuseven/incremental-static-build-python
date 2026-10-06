"""Filesystem helpers and durable source edits."""

import os

import pytest

from isg.fsutil import (
    UnsafePathError,
    atomic_write,
    hash_bytes,
    iter_files,
    normalize_rel,
    safe_join,
)
from isg.model import Edit, Op
from isg.sources import SourceStore


def test_normalize_rel_rejects_traversal_and_absolute():
    assert normalize_rel("a/./b//c/") == "a/b/c"
    with pytest.raises(UnsafePathError):
        normalize_rel("../x")
    with pytest.raises(UnsafePathError):
        normalize_rel("/abs/path")
    with pytest.raises(UnsafePathError):
        normalize_rel("a/../../b")


def test_safe_join_cannot_escape_root():
    root = "/tmp/root"
    assert safe_join(root, "a/b").startswith(root)
    with pytest.raises(UnsafePathError):
        safe_join(root, "../escape")


def test_atomic_write_and_iter_files(tmp_path):
    target = tmp_path / "sub" / "f.txt"
    atomic_write(str(target), b"hello")
    assert target.read_bytes() == b"hello"
    assert iter_files(str(tmp_path)) == ["sub/f.txt"]
    assert hash_bytes(b"hello") == hash_bytes(b"hello")


def test_source_store_scan_snapshot_and_idempotent_edits(dirs):
    src, _out = dirs
    store = SourceStore(src)
    store.apply_edits([
        Edit("content/a.md", Op.UPSERT, b"a"),
        Edit("templates/page.html", Op.UPSERT, b"tpl"),
        Edit("static/x.css", Op.UPSERT, b"css"),
    ])
    scanned = store.scan()
    assert set(scanned) == {
        "content/a.md", "templates/page.html", "static/x.css"}
    assert store.read("content/a.md") == b"a"
    snapshot = store.snapshot()
    assert snapshot["templates/page.html"] == b"tpl"

    # Re-applying identical upserts changes no hashes.
    scanned2 = store.scan()
    store.apply_edits([Edit("content/a.md", Op.UPSERT, b"a")])
    assert store.scan() == scanned2 == scanned

    # Delete is idempotent and removes the file and empty leaf dirs.
    store.apply_edits([Edit("content/a.md", Op.DELETE)])
    assert store.read("content/a.md") is None
    store.apply_edits([Edit("content/a.md", Op.DELETE)])
    assert "content/a.md" not in store.scan()
    assert not os.path.isdir(os.path.join(src, "content", "a.md"))


def test_source_store_rejects_unsafe_edit_paths(dirs):
    src, _out = dirs
    store = SourceStore(src)
    with pytest.raises(UnsafePathError):
        store.apply_edits([Edit("../evil", Op.UPSERT, b"x")])
