"""Failure containment, idempotent retries and interrupted-build recovery."""

import os

import isg.engine as engine_mod

from isg.model import Edit, Op

from .conftest import write


def _start(engine):
    result = engine.start()
    assert result.success
    return engine


def test_failed_page_does_not_pollute_published_version(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "templates/broken.html",
          b"<html>{{data:missing.json:x}}{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/a.md", b"# A")
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    good = engine.site_path()
    good_html = open(os.path.join(good, "a/index.html"), "rb").read()

    result = engine.submit_edits([
        Edit("content/a.md", Op.UPSERT, b"{{template:broken}}\n# A"),
        Edit("content/b.md", Op.UPSERT, b"# B"),
    ])
    assert not result.success
    assert "a" in result.failed and "b" not in result.failed
    assert engine.site_path() == good, "published version moved on failure"
    assert open(os.path.join(good, "a/index.html"), "rb").read() == good_html
    assert not os.path.exists(os.path.join(good, "b/index.html"))
    leftovers = [n for n in os.listdir(engine.releases.releases_dir)
                 if n.startswith(".staging")]
    assert leftovers == []


def test_successful_retry_builds_all_missing_pages_exactly_once(
        make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "templates/broken.html",
          b"<html>{{data:missing.json:x}}{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/a.md", b"# A")
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    engine.submit_edits([
        Edit("content/a.md", Op.UPSERT, b"{{template:broken}}\n# A"),
        Edit("content/b.md", Op.UPSERT, b"# B"),
    ])
    result = engine.submit_edits(
        [Edit("content/a.md", Op.UPSERT, b"# A fixed")])
    assert result.success, result
    site = engine.site_path()
    assert b"A fixed" in open(os.path.join(site, "a/index.html"), "rb").read()
    assert os.path.isfile(os.path.join(site, "b/index.html"))
    # Retrying again with identical content rebuilds nothing.
    again = engine.submit_edits(
        [Edit("content/a.md", Op.UPSERT, b"# A fixed")])
    assert again.success and again.ok == {}


def test_timeout_is_isolated_and_recovers(make_engine, dirs, monkeypatch):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/a.md", b"# A")
    engine = _start(make_engine(page_timeout=0.3, full_rebuild_threshold=1.0))
    good = engine.site_path()

    import time

    original = engine_mod.render_page

    def slow(page_path, snapshot):
        if page_path == "content/a.md":
            time.sleep(1.0)
        return original(page_path, snapshot)

    monkeypatch.setattr(engine_mod, "render_page", slow)
    result = engine.submit_edits([
        Edit("content/a.md", Op.UPSERT, b"# A slow"),
        Edit("content/b.md", Op.UPSERT, b"# B fast"),
    ])
    assert not result.success
    assert result.timed_out == ["a"]
    assert "b" in result.ok
    assert engine.site_path() == good
    leftovers = [n for n in os.listdir(engine.releases.releases_dir)
                 if n.startswith(".staging")]
    assert leftovers == []

    monkeypatch.setattr(engine_mod, "render_page", original)
    result = engine.submit_edits([
        Edit("content/a.md", Op.UPSERT, b"# A fast now"),
        Edit("content/b.md", Op.UPSERT, b"# B fast"),
    ])
    assert result.success
    site = engine.site_path()
    assert b"A fast now" in open(os.path.join(site, "a/index.html"), "rb").read()
    assert b"B fast" in open(os.path.join(site, "b/index.html"), "rb").read()


def test_restart_recovers_staging_leftovers_and_keeps_current(make_engine, dirs):
    src, out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    engine = _start(make_engine())
    current_id = engine.releases.current_release_id()

    # Simulate a crash mid-build: a stale staging directory is left behind.
    stale = os.path.join(engine.releases.releases_dir, ".staging-crash")
    os.makedirs(stale)
    with open(os.path.join(stale, "partial.html"), "wb") as fh:
        fh.write(b"half-written")

    from isg.engine import Engine
    restarted = Engine(src, out, engine.config)
    result = restarted.start()
    assert result.success
    assert restarted.releases.current_release_id() == current_id
    assert not os.path.exists(stale)


def test_deleting_dependency_file_fails_build_and_restore_converges(
        make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html",
          b"<html>{{data:data/site.json:title}}{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/data/site.json", b'{"title": "T1"}')
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    site1 = engine.site_path()
    assert b"T1" in open(os.path.join(site1, "index.html"), "rb").read()

    result = engine.submit_edits(
        [Edit("content/data/site.json", Op.DELETE)])
    assert not result.success
    assert "" in result.failed
    assert engine.site_path() == site1

    result = engine.submit_edits(
        [Edit("content/data/site.json", Op.UPSERT, b'{"title": "T2"}')])
    assert result.success
    assert list(result.ok) == [""]
    site2 = engine.site_path()
    html = open(os.path.join(site2, "index.html"), "rb").read()
    assert b"T2" in html and b"T1" not in html


def test_restart_without_changes_republishes_nothing(make_engine, dirs):
    src, out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    engine = _start(make_engine())
    current = engine.releases.current_release_id()

    from isg.engine import Engine
    restarted = Engine(src, out, engine.config)
    result = restarted.start()
    assert result.success
    assert result.ok == {} and result.failed == {}
    assert restarted.releases.current_release_id() == current
