"""Source deletion cleanup: outputs, directories, index entries, old files."""

import json
import os

from isg.model import Edit, Op

from .conftest import write


def _start(engine):
    result = engine.start()
    assert result.success
    return engine


def test_deleted_page_output_and_manifest_entries_are_removed(
        make_engine, basic_site):
    engine = _start(make_engine())
    result = engine.submit_edits([Edit("content/a.md", Op.DELETE)])
    assert result.success
    site = engine.site_path()
    assert not os.path.exists(os.path.join(site, "a"))
    manifest = json.load(
        open(os.path.join(site, "manifest.json"), encoding="utf-8"))
    assert "a" not in manifest["pages"]
    assert "a" not in manifest["deps"]
    # Home page is unaffected.
    assert "" in manifest["pages"]
    assert os.path.isfile(os.path.join(site, "index.html"))


def test_deleted_nested_page_removes_leaf_directory(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/guides/deep.md", b"# Deep")
    write(src, "content/guides/keep.md", b"# Keep")
    engine = _start(make_engine())
    engine.submit_edits([Edit("content/guides/deep.md", Op.DELETE)])
    site = engine.site_path()
    assert not os.path.exists(os.path.join(site, "guides/deep"))
    # Sibling page keeps the guides directory alive.
    assert os.path.isfile(os.path.join(site, "guides/keep/index.html"))


def test_deleted_asset_is_removed(make_engine, basic_site):
    engine = _start(make_engine())
    assert os.path.isfile(os.path.join(engine.site_path(), "app.css"))
    engine.submit_edits([Edit("static/app.css", Op.DELETE)])
    site = engine.site_path()
    assert not os.path.exists(os.path.join(site, "app.css"))
    manifest = json.load(
        open(os.path.join(site, "manifest.json"), encoding="utf-8"))
    assert "static/app.css" not in manifest["assets"]


def test_old_releases_do_not_accumulate_by_default(make_engine, basic_site):
    engine = _start(make_engine(keep_releases=0))
    for i in range(4):
        engine.submit_edits(
            [Edit("content/a.md", Op.UPSERT, f"# A\n\nv{i}".encode())])
    releases = engine.releases.list_releases()
    assert len(releases) == 1
    assert releases[0][2] is True  # referenced by current
    assert not [
        n for n in os.listdir(engine.releases.releases_dir)
        if n.startswith(".")
    ]


def test_deleted_template_page_removed_when_template_disappears(make_engine, dirs):
    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    write(src, "content/index.md", b"# Home")
    write(src, "content/a.md", b"{{template:alt}}\n# A")
    write(src, "templates/alt.html", b"<alt>{{content}}</alt>")
    engine = _start(make_engine())
    # Deleting the alt template makes page a fail to render; the previous
    # complete release must remain published.
    engine.submit_edits([Edit("templates/alt.html", Op.DELETE)])
    # Page a itself still exists; deleting its source cleans the output.
    result = engine.submit_edits([Edit("content/a.md", Op.DELETE)])
    assert result.success
    site = engine.site_path()
    assert not os.path.exists(os.path.join(site, "a"))
