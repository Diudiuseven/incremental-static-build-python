"""Incremental planning: dependency invalidation, convergence, fallbacks."""

from isg.manifest import Manifest
from isg.planner import plan_build


def H(s: bytes) -> str:
    import hashlib

    return hashlib.blake2b(s, digest_size=16).hexdigest()


KEYS = ("", "a", "b", "c")


def _src(key):
    return "content/index.md" if key == "" else f"content/{key}.md"


def _fresh():
    cur = {_src(k): H((k or "home").encode()) for k in KEYS}
    cur["templates/page.html"] = H(b"<html>{{content}}</html>")
    cur["static/app.css"] = H(b"css")

    def fp(deps):
        import hashlib

        h = hashlib.blake2b(digest_size=16)
        for d in sorted(deps):
            h.update(d.encode())
            h.update(b"=")
            h.update(cur[d].encode())
            h.update(b";")
        return h.hexdigest()

    m = Manifest(
        "r1",
        pages={k: fp([_src(k), "templates/page.html"]) for k in KEYS},
        deps={k: [_src(k), "templates/page.html"] for k in KEYS},
        assets={"static/app.css": H(b"css")},
    )
    return cur, m


def test_no_changes_means_no_dirty_pages():
    cur, m = _fresh()
    p = plan_build(manifest=m, current_hashes=cur, changed=frozenset(),
                   deleted=frozenset(), threshold=0.5, batch_size=100)
    assert not p.full
    assert p.dirty_pages == frozenset()


def test_page_content_change_only_dirties_that_page():
    cur, m = _fresh()
    cur["content/a.md"] = H(b"a v2")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["content/a.md"]), deleted=frozenset(),
                   threshold=0.5, batch_size=100)
    assert not p.full
    assert p.dirty_pages == frozenset({"a"})


def test_template_change_invalidates_all_dependent_pages():
    cur, m = _fresh()
    cur["templates/page.html"] = H(b"<body>{{content}}</body>")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["templates/page.html"]),
                   deleted=frozenset(), threshold=1.0, batch_size=100)
    assert not p.full
    assert p.dirty_pages == frozenset(KEYS)


def test_deleted_page_is_listed_for_cleanup():
    cur, m = _fresh()
    del cur["content/a.md"]
    p = plan_build(manifest=m, current_hashes=cur, changed=frozenset(),
                   deleted=frozenset(["content/a.md"]),
                   threshold=0.5, batch_size=100)
    assert p.deleted_pages == frozenset({"a"})
    assert "a" not in p.dirty_pages


def test_new_page_is_dirty():
    cur, m = _fresh()
    cur["content/new.md"] = H(b"new")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["content/new.md"]), deleted=frozenset(),
                   threshold=0.5, batch_size=100)
    assert "new" in p.dirty_pages


def test_changed_asset_is_reported():
    cur, m = _fresh()
    cur["static/app.css"] = H(b"css2")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["static/app.css"]), deleted=frozenset(),
                   threshold=0.5, batch_size=100)
    assert p.changed_assets == frozenset({"static/app.css"})
    assert p.dirty_pages == frozenset()


def test_first_build_is_full():
    cur, _m = _fresh()
    assert plan_build(manifest=None, current_hashes=cur, changed=frozenset(),
                      deleted=frozenset(), threshold=0.5,
                      batch_size=100).full


def test_threshold_falls_back_to_full():
    cur, m = _fresh()
    cur["templates/page.html"] = H(b"<body>{{content}}</body>")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["templates/page.html"]),
                   deleted=frozenset(), threshold=0.5, batch_size=100)
    assert p.full
    assert "templates/page.html" in p.changed_inputs


def test_ratio_equal_threshold_stays_incremental():
    cur, m = _fresh()
    cur["templates/page.html"] = H(b"<body>{{content}}</body>")
    p = plan_build(manifest=m, current_hashes=cur,
                   changed=frozenset(["templates/page.html"]),
                   deleted=frozenset(), threshold=1.0, batch_size=100)
    assert not p.full


def test_out_of_order_or_noisy_change_reports_converge():
    """The plan is a function of the current tree, not of the edit history."""
    cur, m = _fresh()
    cur["content/a.md"] = H(b"a v2")
    p_noisy = plan_build(
        manifest=m, current_hashes=cur,
        changed=frozenset(["content/a.md", "content/ghost.md", "templates/x"]),
        deleted=frozenset(), threshold=1.0, batch_size=100)
    p_empty = plan_build(
        manifest=m, current_hashes=cur, changed=frozenset(),
        deleted=frozenset(), threshold=1.0, batch_size=100)
    assert p_noisy.dirty_pages == p_empty.dirty_pages == frozenset({"a"})


def test_batch_size_overflow_falls_back():
    cur, m = _fresh()
    cur["content/a.md"] = H(b"a v2")
    p = plan_build(manifest=m, current_hashes=cur, changed=frozenset(),
                   deleted=frozenset(), threshold=1.0, batch_size=0)
    assert p.full
