"""End-to-end incremental build semantics."""

import json
import os

from isg.model import Edit, Op

from .conftest import write


def _start(engine):
    result = engine.start()
    assert result.success, result
    return engine


def _read(engine, rel):
    with open(os.path.join(engine.site_path(), rel), "rb") as fh:
        return fh.read()


def _manifest(engine):
    return json.loads(_read(engine, "manifest.json"))


def test_initial_build_publishes_complete_site(make_engine, basic_site):
    engine = _start(make_engine())
    assert b"<h1>Home</h1>" in _read(engine, "index.html")
    assert b"body-A-v1" in _read(engine, "a/index.html")
    assert b"<head>H</head>" in _read(engine, "a/index.html")
    assert _read(engine, "app.css") == b"body{}"
    pages = _manifest(engine)["pages"]
    assert set(pages) == {"", "a"}


def test_page_edit_rebuilds_only_that_page(make_engine, basic_site):
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    first = engine.site_path()
    result = engine.submit_edits(
        [Edit("content/a.md", Op.UPSERT, b"# A page\n\nbody-A-v2")])
    assert result.success
    assert list(result.ok) == ["a"]
    assert engine.site_path() != first
    assert b"body-A-v2" in _read(engine, "a/index.html")
    assert b"body-A-v1" not in _read(engine, "a/index.html")
    # Unchanged home page is reused but still present in the new release.
    assert b"<h1>Home</h1>" in _read(engine, "index.html")


def test_partial_template_change_invalidates_dependent_pages(
        make_engine, basic_site):
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    result = engine.submit_edits(
        [Edit("templates/partials/h.html", Op.UPSERT, b"<head>H2</head>")])
    assert result.success
    assert set(result.ok) == {"", "a"}
    assert b"<head>H2</head>" in _read(engine, "a/index.html")
    assert b"<head>H2</head>" in _read(engine, "index.html")


def test_data_file_change_rebuilds_consumers(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html",
          b"<html>{{data:data/site.json:title}}{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/data/site.json", b'{"title": "One"}')
    engine = _start(make_engine(full_rebuild_threshold=1.0))
    assert b"One" in _read(engine, "index.html")
    result = engine.submit_edits(
        [Edit("content/data/site.json", Op.UPSERT, b'{"title": "Two"}')])
    assert result.success and list(result.ok) == [""]
    assert b"Two" in _read(engine, "index.html") and b"One" not in _read(engine, "index.html")


def test_repeated_identical_edits_do_not_rebuild(make_engine, basic_site):
    engine = _start(make_engine())
    result1 = engine.submit_edits(
        [Edit("content/a.md", Op.UPSERT, b"# A page\n\nbody-A-v1")])
    assert result1.success
    result2 = engine.submit_edits(
        [Edit("content/a.md", Op.UPSERT, b"# A page\n\nbody-A-v1")])
    assert result2.success and result2.ok == {} and result2.failed == {}


def test_out_of_order_edits_converge_to_full_rebuild_result(make_engine, basic_site):
    engine_a = _start(make_engine(full_rebuild_threshold=1.0))
    final_content = b"# A page\n\nfinal-body"
    # Apply v2 then v1 then final out of the "intended" order, repeatedly.
    for content in (b"# A page\n\nv2", b"# A page\n\nv1", final_content,
                    final_content, b"# A page\n\nv2", final_content):
        result = engine_a.submit_edits([Edit("content/a.md", Op.UPSERT, content)])
        assert result.success
    assert b"final-body" in _read(engine_a, "a/index.html")
    assert b"<h1>A page</h1>" in _read(engine_a, "a/index.html")

    # Reference engine: only ever applies the final content from scratch.
    from isg.engine import Engine
    from isg.config import Config
    ref_out = os.path.join(os.path.dirname(engine_a.releases.output_root), "ref")
    ref = Engine(engine_a.sources.root, ref_out, Config(full_rebuild_threshold=1.0))
    ref.start()
    assert _read(ref, "a/index.html") == _read(engine_a, "a/index.html")
    assert _read(ref, "index.html") == _read(engine_a, "index.html")
