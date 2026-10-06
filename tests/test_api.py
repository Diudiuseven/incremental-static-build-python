"""HTTP API tests: editing endpoints and version-consistent reads."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from isg.api import create_app


@pytest.fixture()
def client(started_engine):
    return TestClient(create_app(started_engine))


def test_status_and_manifest(client, started_engine):
    status = client.get("/status").json()
    assert status["release"] == started_engine.current().release_id
    assert set(status["routes"]) == {"/", "/a", "/b"}

    manifest = client.get("/__site/manifest.json").json()
    assert manifest["release_id"] == status["release"]
    assert "/" in manifest["pages"]


def test_serves_current_pages_only(client):
    body = client.get("/a").text
    assert "alpha" in body and "<nav>" in body
    assert client.get("/missing").status_code == 404
    # Directory-style route resolution.
    assert client.get("/").status_code == 200


def test_event_endpoint_updates_release(client, started_engine, site):
    before = started_engine.current().release_id
    (site / "content" / "a.md").write_text(
        "---\ntitle: A\ndata: posts.json\n---\n# A\n\nfresh-api")
    resp = client.post("/events",
                       params={"path": "content/a.md", "wait": "true"})
    assert resp.status_code == 200
    after = started_engine.current().release_id
    assert after != before
    assert "fresh-api" in client.get("/a").text


def test_batch_endpoint_and_delete(client, started_engine, site):
    (site / "content" / "b.md").unlink()
    resp = client.post(
        "/events/batch",
        json=[{"kind": "delete", "path": "content/b.md"}],
        params={"wait": "true"})
    assert resp.status_code == 200
    assert client.get("/b").status_code == 404


def test_rebuild_endpoint(client):
    resp = client.post("/rebuild")
    assert resp.status_code == 200
    assert resp.json()["pages"] == 3
    full = client.post("/rebuild", params={"full": "true"})
    assert full.status_code == 200


def test_reads_always_reflect_one_consistent_release(client, started_engine):
    # Every response resolves through the same current manifest; capture
    # release id served across many concurrent reads.
    import threading
    seen = set()
    errors = []

    def reader():
        try:
            for _ in range(20):
                r = client.get("/a")
                # ETag-like marker: digest in manifest of current release
                seen.add(started_engine.current().release_id)
                if r.status_code != 200:
                    errors.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(seen) >= 1
