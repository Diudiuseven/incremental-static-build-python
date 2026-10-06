"""Unit tests for the coalescing queue and dependency planner."""

from __future__ import annotations

from isg.model import ChangeEvent, ChangeKind
from isg.planner import (DependencyGraph, Planner, content_key,
                         event_dependency_key, partial_key, template_key)
from isg.queue import CoalescingQueue
from isg.sources import read_source_tree


def test_queue_deduplicates_and_sorts():
    q = CoalescingQueue(batch_size=10)
    q.put(ChangeEvent(ChangeKind.UPSERT, "content/z.md"))
    q.put(ChangeEvent(ChangeKind.UPSERT, "content/z.md"))  # repeat
    q.put(ChangeEvent(ChangeKind.UPSERT, "content/a.md"))
    q.put(ChangeEvent(ChangeKind.DELETE, "content/m.md"))
    drained = q.drain()
    assert [e.path for e in drained] == [
        "content/a.md", "content/m.md", "content/z.md"]
    assert drained[1].kind is ChangeKind.DELETE
    assert len(q) == 0


def test_queue_last_event_wins_and_batches():
    q = CoalescingQueue(batch_size=2)
    q.put(ChangeEvent(ChangeKind.UPSERT, "x"))
    q.put(ChangeEvent(ChangeKind.DELETE, "x"))
    assert q.drain()[0].kind is ChangeKind.DELETE

    for i in range(5):
        q.put(ChangeEvent(ChangeKind.UPSERT, f"p{i}"))
    first = q.drain()
    assert len(first) == 2
    assert len(q) == 3


def test_event_key_mapping():
    assert event_dependency_key("content/foo/x.md") == content_key(
        "content/foo/x.md")
    assert event_dependency_key("templates/page.html") == template_key(
        "page.html")
    assert event_dependency_key("templates/_nav.html") == partial_key(
        "_nav.html")


def test_graph_fanout_and_removal():
    g = DependencyGraph()
    g.set_deps("/a", {content_key("content/a.md"), partial_key("_n.html")})
    g.set_deps("/b", {content_key("content/b.md"), partial_key("_n.html")})
    assert g.dependents(partial_key("_n.html")) == {"/a", "/b"}
    g.remove_route("/a")
    assert g.dependents(partial_key("_n.html")) == {"/b"}
    assert g.all_routes() == {"/b"}


def test_planner_partial_change_marks_dependents(site):
    # A fourth page with a different template does not include the partial.
    (site / "templates" / "plain.html").write_text("<p>{{{body_html}}}</p>")
    (site / "content" / "c.md").write_text(
        "---\ntemplate: plain.html\n---\n# C\n\nc")
    snapshot = read_source_tree(site)
    graph = DependencyGraph()
    p = Planner(graph)
    analyses = {a.route: a for a in p.analyze_snapshot(snapshot)}
    from isg.render import Renderer
    renderer = Renderer(snapshot.templates)
    partials = renderer.partials()
    for route, analysis in analyses.items():
        inc = renderer.discover_includes(analysis.template)
        graph.set_deps(route, p.discover_dependencies(analysis, inc, partials))

    plan = p.plan(
        [ChangeEvent(ChangeKind.UPSERT, "templates/_nav.html")],
        snapshot, total_pages=4, threshold=1.0)
    assert plan.routes_to_build == {"/", "/a", "/b"}
    assert "/c" not in plan.routes_to_build
    assert not plan.full_rebuild


def test_planner_delete_marks_route(site):
    # Snapshot reflects the post-delete state: content/a.md is gone.
    (site / "content" / "a.md").unlink()
    snapshot = read_source_tree(site)
    graph = DependencyGraph()
    graph.set_deps("/a", {content_key("content/a.md"),
                          template_key("page.html")})
    planner = Planner(graph)
    plan = planner.plan(
        [ChangeEvent(ChangeKind.DELETE, "content/a.md")],
        snapshot, total_pages=3, threshold=1.0)
    assert "/a" in plan.routes_to_delete
    assert "/a" not in plan.routes_to_build


def test_planner_reorder_independence(site):
    snapshot = read_source_tree(site)
    graph = DependencyGraph()
    planner = Planner(graph)
    upsert = ChangeEvent(ChangeKind.UPSERT, "content/a.md")
    # Content still exists on disk: both orders yield a build, no delete.
    p1 = planner.plan([upsert], snapshot, 3, 1.0)
    p2 = planner.plan([upsert, upsert], snapshot, 3, 1.0)
    assert p1.routes_to_build == p2.routes_to_build == {"/a"}


def test_planner_full_rebuild_threshold(site):
    snapshot = read_source_tree(site)
    graph = DependencyGraph()
    for route in ("/", "/a", "/b"):
        graph.set_deps(route, {content_key("content/x.md"),
                               partial_key("_nav.html")})
    planner = Planner(graph)
    plan = planner.plan(
        [ChangeEvent(ChangeKind.UPSERT, "templates/_nav.html")],
        snapshot, total_pages=3, threshold=0.5)
    assert plan.full_rebuild
    assert plan.routes_to_build == {"/", "/a", "/b"}
