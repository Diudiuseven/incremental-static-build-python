"""HTTP surface: published reads, edit API, status and error responses."""

import pytest

from fastapi.testclient import TestClient

from isg.api import create_app
from isg.model import Edit, Op

from .conftest import write


@pytest.fixture
def client(make_engine, basic_site):
    engine = make_engine(full_rebuild_threshold=1.0)
    engine.start()
    return TestClient(create_app(engine)), engine


def test_read_published_pages_and_directory_indexes(client):
    http, engine = client
    assert b"<h1>Home</h1>" in http.get("/").content
    assert b"body-A-v1" in http.get("/a/").content
    assert http.get("/missing/").status_code == 404
    # Static asset served from the current release.
    assert http.get("/app.css").content == b"body{}"


def test_edit_then_read_serves_new_release(client):
    http, engine = client
    response = http.post("/__admin/edits", json={"edits": [
        {"path": "content/a.md", "op": "upsert",
         "content": "# A page\n\nbrand-new"}]})
    assert response.status_code == 200
    assert response.json()["build"]["ok"] == ["a"]
    assert b"brand-new" in http.get("/a/").content


def test_delete_returns_404_afterwards(client):
    http, _engine = client
    assert http.post("/__admin/edits", json={"edits": [
        {"path": "content/a.md", "op": "delete"}]}).status_code == 200
    assert http.get("/a/").status_code == 404


def test_failed_batch_returns_409_without_changing_reads(client):
    http, _engine = client
    response = http.post("/__admin/edits", json={"edits": [
        {"path": "templates/bad.html", "op": "upsert",
         "content": "{{data:missing.json:x}}"},
        {"path": "content/a.md", "op": "upsert",
         "content": "{{template:bad}}\n# A"}]})
    assert response.status_code == 409
    assert set(response.json()["build"]["failed"]) == {"a"}
    assert b"body-A-v1" in http.get("/a/").content


def test_unsafe_paths_rejected(client):
    http, _engine = client
    response = http.post("/__admin/edits", json={"edits": [
        {"path": "../escape.md", "op": "upsert", "content": "x"}]})
    assert response.status_code == 400


def test_status_reports_current_release(client):
    http, engine = client
    payload = http.get("/__admin/status").json()
    assert payload["current_release_id"] == engine.releases.current_release_id()
    assert payload["last_build"] is not None


def test_full_rebuild_endpoint(client, dirs):
    http, engine = client
    src, _out = dirs
    write(src, "content/new.md", b"# New")
    response = http.post("/__admin/rebuild")
    assert response.status_code == 200
    assert b"<h1>New</h1>" in http.get("/new/").content
