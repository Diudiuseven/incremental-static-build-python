"""Concurrent edits and concurrent full/incremental build determinism."""

import json
import os
import threading

from isg.model import Edit, Op

from .conftest import write


def _start(engine):
    result = engine.start()
    assert result.success
    return engine


def test_many_concurrent_editors_converge_to_source_state(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html><body>{{content}}</body></html>")
    write(src, "content/index.md", b"# Home")
    engine = _start(make_engine(concurrency=4, batch_size=1000,
                                full_rebuild_threshold=0.9))
    errors: list[Exception] = []

    def editor(i: int, n: int) -> None:
        try:
            for j in range(n):
                if j == n - 1 and i % 3 == 0:
                    engine.submit_edits([Edit(f"content/p{i}.md", Op.DELETE)])
                else:
                    engine.submit_edits([
                        Edit(f"content/p{i}.md", Op.UPSERT,
                             f"# p{i} v{j}".encode()),
                        Edit("content/shared.md", Op.UPSERT,
                             f"# shared {i} {j}".encode()),
                    ])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=editor, args=(i, 10))
               for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    site = engine.site_path()
    manifest = json.load(
        open(os.path.join(site, "manifest.json"), encoding="utf-8"))
    for i in range(8):
        output = os.path.join(site, f"p{i}/index.html")
        if i % 3 == 0:
            assert not os.path.exists(output)
            assert f"p{i}" not in manifest["pages"]
        else:
            assert os.path.isfile(output)
            assert f"<h1>p{i} v9</h1>".encode() in open(output, "rb").read()
    # The shared page reflects exactly one of the final writes.
    shared = open(os.path.join(src, "content/shared.md"), "rb").read()[2:]
    assert shared.decode() in open(
        os.path.join(site, "shared/index.html"), "rb").read().decode()
    # Home page never corrupted.
    assert b"<h1>Home</h1>" in open(
        os.path.join(site, "index.html"), "rb").read()
    # One self-consistent release only, no staging garbage.
    referenced = [n for n, _c, r in engine.releases.list_releases() if r]
    assert len(referenced) == 1
    assert not [n for n in os.listdir(engine.releases.releases_dir)
                if n.startswith(".staging")]


def test_full_and_incremental_paths_produce_identical_site(tmp_path):
    from isg.config import Config
    from isg.engine import Engine

    def build(threshold: float) -> Engine:
        src = tmp_path / f"src-{threshold}"
        out = tmp_path / f"out-{threshold}"
        src.mkdir()
        out.mkdir()
        write(str(src), "templates/page.html",
              b"<html>{{include:p/h.html}}{{content}}</html>")
        write(str(src), "templates/p/h.html", b"<head>x</head>")
        for key in ("", "a", "b", "c", "d"):
            rel = "content/index.md" if key == "" else f"content/{key}.md"
            write(str(src), rel, f"# {key or 'home'}".encode())
        engine = Engine(str(src), str(out),
                        Config(full_rebuild_threshold=threshold, batch_size=100))
        engine.start()
        engine.submit_edits([
            Edit("templates/p/h.html", Op.UPSERT, b"<head>y</head>")])
        return engine

    incremental = build(1.0)
    full = build(0.1)
    assert incremental.last_plan_reason == ""
    assert full.last_plan_reason  # a fallback reason was recorded

    def artifacts(engine):
        found = {}
        site = engine.site_path()
        for root, _dirs, files in os.walk(site):
            for name in files:
                if name == "manifest.json":
                    continue  # release_id differs by design; compared below
                path = os.path.join(root, name)
                rel = os.path.relpath(path, site)
                found[rel] = open(path, "rb").read()
        return found

    art_i = artifacts(incremental)
    art_f = artifacts(full)
    assert set(art_i) == set(art_f)
    for rel, content in art_i.items():
        assert content == art_f[rel], rel

    import json as _json

    def load_manifest(engine):
        return _json.load(
            open(os.path.join(engine.site_path(), "manifest.json"),
                 encoding="utf-8"))

    mi = load_manifest(incremental)
    mf = load_manifest(full)
    assert mi["release_id"] and mf["release_id"]
    assert mi["pages"] == mf["pages"]
    assert mi["deps"] == mf["deps"]
    assert mi["assets"] == mf["assets"]


def test_concurrent_full_rebuild_and_edits_are_serialized(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    engine = _start(make_engine(concurrency=2, full_rebuild_threshold=1.0))
    errors: list[Exception] = []

    def hammer_edits():
        try:
            for i in range(10):
                engine.submit_edits(
                    [Edit("content/x.md", Op.UPSERT, f"# X{i}".encode())])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def hammer_rebuild():
        try:
            for _ in range(5):
                engine.rebuild_full()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=hammer_edits)
    t2 = threading.Thread(target=hammer_rebuild)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert errors == []
    site = engine.site_path()
    html = open(os.path.join(site, "x/index.html"), "rb").read()
    assert html in [f"<html><h1>X{i}</h1></html>".encode() for i in range(10)]
    assert os.path.isfile(os.path.join(site, "index.html"))
