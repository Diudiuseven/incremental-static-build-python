"""Integration tests for incremental builds and publish semantics."""

from __future__ import annotations

import threading
from pathlib import Path


from isg.config import Config
from isg.engine import Engine
from isg.model import ChangeEvent, ChangeKind



def _upsert(rel: str) -> ChangeEvent:
    return ChangeEvent(ChangeKind.UPSERT, rel)


def _delete(rel: str) -> ChangeEvent:
    return ChangeEvent(ChangeKind.DELETE, rel)


def _edit_page(site: Path, name: str, body: str,
               title: str | None = None, data: bool = True) -> None:
    header = f"---\ntitle: {title or name}\n"
    if data:
        header += "data: posts.json\n"
    header += "---\n"
    (site / "content" / name).write_text(header + f"# {title or name}\n\n{body}")


def test_initial_full_publish(started_engine: Engine):
    info = started_engine.current()
    assert info is not None
    assert set(info.pages) == {"/", "/a", "/b"}
    assert started_engine.read_current_file("/").startswith(b"<html>")


def test_incremental_only_rebuilds_changed_page(started_engine: Engine, site):
    before = started_engine.current()
    _edit_page(site, "a.md", "brand-new-alpha")
    started_engine.submit(_upsert("content/a.md"))
    assert started_engine.wait_idle(5)
    after = started_engine.current()
    assert after.release_id != before.release_id
    assert after.pages["/a"][0] != before.pages["/a"][0]
    # Unchanged pages keep identical artifacts.
    assert after.pages["/b"][0] == before.pages["/b"][0]
    assert after.pages["/"][0] == before.pages["/"][0]
    assert b"brand-new-alpha" in started_engine.read_current_file("/a")


def test_repeated_changes_converge(started_engine: Engine, site):
    before = {r: v[0] for r, v in started_engine.current().pages.items()}
    for i in range(5):
        _edit_page(site, "a.md", f"version-{i}")
        started_engine.submit(_upsert("content/a.md"))
    assert started_engine.wait_idle(5)
    after = {r: v[0] for r, v in started_engine.current().pages.items()}
    assert b"version-4" in started_engine.read_current_file("/a")
    assert after["/b"] == before["/b"]


def test_out_of_order_events_converge_to_snapshot(started_engine: Engine, site):
    # Events arrive "upsert new content" then "delete", but disk ends
    # without the file; result must equal a clean delete.
    _edit_page(site, "new.md", "fresh")
    started_engine.submit_many([
        _upsert("content/new.md"),
        _upsert("content/new.md"),
        _delete("content/new.md"),
    ])
    (site / "content" / "new.md").unlink()
    assert started_engine.wait_idle(5)
    assert "/new" not in started_engine.current().pages

    # Reverse burst: delete first, then recreate -> final state exists.
    _edit_page(site, "new.md", "resurrected")
    started_engine.submit_many([
        _delete("content/new.md"),
        _upsert("content/new.md"),
    ])
    assert started_engine.wait_idle(5)
    assert "/new" in started_engine.current().pages
    assert b"resurrected" in started_engine.read_current_file("/new")


def test_partial_change_invalidates_all_including_pages(started_engine, site):
    before = started_engine.current()
    (site / "templates" / "_nav.html").write_text("<nav>NEW-NAV</nav>")
    started_engine.submit(_upsert("templates/_nav.html"))
    assert started_engine.wait_idle(5)
    after = started_engine.current()
    for route in ("/", "/a", "/b"):
        assert after.pages[route][0] != before.pages[route][0]
    assert b"NEW-NAV" in started_engine.read_current_file("/b")


def test_data_change_invalidates_declaring_pages(started_engine, site):
    before = started_engine.current()
    (site / "data" / "posts.json").write_text(
        '[{"title":"p1"},{"title":"p2"},{"title":"p3"}]')
    started_engine.submit(_upsert("data/posts.json"))
    assert started_engine.wait_idle(5)
    after = started_engine.current()
    assert after.pages["/a"][0] != before.pages["/a"][0]
    assert b"<span>p3</span>" in started_engine.read_current_file("/a")


def test_delete_cleans_artifact_and_index(started_engine, site, tmp_path):
    (site / "content" / "a.md").unlink()
    started_engine.submit(_delete("content/a.md"))
    assert started_engine.wait_idle(5)
    info = started_engine.current()
    assert "/a" not in info.pages
    assert "/a" not in info.deps
    assert started_engine.read_current_file("/a") is None
    # The artifact file is absent from the published release tree.
    release_dir = started_engine.store.current_dir()
    assert not (release_dir / "a.html").exists()
    assert (release_dir / "b.html").exists()


def test_failed_page_keeps_published_release(started_engine, site):
    good = started_engine.current()
    # Reference a missing variable/data to force a render failure.
    (site / "templates" / "page.html").write_text(
        "<html>{{does_not_exist}}</html>")
    started_engine.submit(_upsert("content/a.md"))
    assert started_engine.wait_idle(5)
    # Published version is untouched and still complete.
    assert started_engine.current().release_id == good.release_id
    assert b"alpha" in started_engine.read_current_file("/a")
    # No half-written staging directories remain.
    assert not any(started_engine.store.staging_dir.iterdir())


def test_retry_after_fix_succeeds_without_duplication(started_engine, site):
    (site / "templates" / "page.html").write_text("<b>{{missing}}</b>")
    started_engine.submit(_upsert("content/a.md"))
    started_engine.wait_idle(5)
    failed_revision = started_engine.current().plan_revision

    (site / "templates" / "page.html").write_text(
        "<html><main>{{{body_html}}}</main></html>")
    started_engine.submit(_upsert("content/a.md"))
    assert started_engine.wait_idle(5)
    info = started_engine.current()
    assert info.plan_revision == failed_revision + 1
    assert b"alpha" in started_engine.read_current_file("/a")
    # Exactly one release beyond the failed attempt; staging empty.
    assert not any(started_engine.store.staging_dir.iterdir())


def test_concurrent_edits_are_deterministic(started_engine, site):
    base = {r: v[0] for r, v in started_engine.current().pages.items()}

    def writer(name: str, count: int) -> None:
        for i in range(count):
            _edit_page(site, name, f"body-{name}-{i}")
            started_engine.submit(_upsert(f"content/{name}"))

    threads = [threading.Thread(target=writer, args=(f"t{n}.md", 20))
               for n in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert started_engine.wait_idle(10)

    final_pages = {r: v[0] for r, v in started_engine.current().pages.items()}
    # All five new pages exist with the final (body-19) content; unrelated
    # original pages are untouched.
    for n in range(5):
        route = f"/t{n}"
        assert route in final_pages
        assert f"body-t{n}.md-19".encode() in \
            started_engine.read_current_file(route)
    for route in ("/", "/a", "/b"):
        assert final_pages[route] == base[route]


def test_lost_delete_event_still_reconciles_to_snapshot(started_engine, site):
    # The file vanishes from disk without any engine notification; an
    # unrelated event must still cause the stale route to be removed.
    (site / "content" / "a.md").unlink()
    started_engine.submit(_upsert("content/b.md"))  # unrelated event
    assert started_engine.wait_idle(5)
    assert "/a" not in started_engine.current().pages
    assert "/b" in started_engine.current().pages


def test_incremental_equals_full_rebuild(started_engine, site, tmp_path):
    # Make scattered edits across a larger site.
    for i in range(8):
        _edit_page(site, f"p{i}.md", f"initial-{i}")
    started_engine.submit_many([_upsert(f"content/p{i}.md")
                                for i in range(8)])
    assert started_engine.wait_idle(10)

    for i in (1, 3, 5):
        _edit_page(site, f"p{i}.md", f"edited-{i}")
    started_engine.submit_many([_upsert("content/p1.md"),
                                _upsert("content/p3.md"),
                                _upsert("content/p5.md")])
    assert started_engine.wait_idle(10)
    incremental = {r: v[0] for r, v in started_engine.current().pages.items()}

    full_info = started_engine.full_rebuild()
    full = {r: v[0] for r, v in full_info.pages.items()}
    assert full == incremental


def test_full_rebuild_fallback_matches_full_path(tmp_path, site):
    eng = Engine(site, tmp_path / "build2",
                 config=Config(concurrency=2, page_timeout=5,
                               full_rebuild_threshold=0.5,
                               batch_idle_seconds=0.01))
    eng.start()
    base = eng.rebuild_now()
    _edit_page(site, "a.md", "aa")
    _edit_page(site, "b.md", "bb")
    eng.submit_many([_upsert("content/a.md"), _upsert("content/b.md")])
    assert eng.wait_idle(5)
    fallback = {r: v[0] for r, v in eng.current().pages.items()}
    explicit = {r: v[0] for r, v in eng.full_rebuild().pages.items()}
    assert fallback == explicit
    assert fallback["/a"] != base.pages["/a"][0]
    eng.stop()


def test_recovery_after_restart_cleans_and_serves(tmp_path, site):
    eng = Engine(site, tmp_path / "build",
                 config=Config(keep_releases=1, batch_idle_seconds=0.01))
    eng.start()
    eng.rebuild_now()
    # Simulate a crashed process: leave staging junk and a half release.
    junk = eng.store.staging_dir / "junk"
    junk.mkdir(parents=True)
    (junk / "x.html").write_text("partial")
    half = eng.store.releases_dir / "incomplete"
    half.mkdir()
    (half / "index.html").write_text("half")
    expected_routes = set(eng.current().pages)
    eng.stop()

    restarted = Engine(site, tmp_path / "build",
                       config=Config(keep_releases=1))
    try:
        assert not any(restarted.store.staging_dir.iterdir())
        assert not (restarted.store.releases_dir / "incomplete").exists()
        assert set(restarted.current().pages) == expected_routes
    finally:
        restarted.stop()


def test_pruning_does_not_break_current_when_keep_zero(tmp_path, site):
    eng = Engine(site, tmp_path / "build",
                 config=Config(concurrency=2, page_timeout=5,
                               full_rebuild_threshold=1.0,
                               keep_releases=0, batch_idle_seconds=0.01))
    eng.start()
    first = eng.rebuild_now()
    _edit_page(site, "a.md", "v2")
    eng.submit(_upsert("content/a.md"))
    assert eng.wait_idle(5)
    # Old release tree removed, but current pages still serve fully.
    assert not (eng.store.releases_dir / first.release_id).exists()
    for route, (_d, obj) in eng.current().pages.items():
        assert eng.read_current_file(route) is not None
    eng.stop()
