"""Shared pytest fixtures: on-disk source trees and configured engines."""

from __future__ import annotations

import os

import pytest

from isg.config import Config
from isg.engine import Engine


@pytest.fixture
def dirs(tmp_path):
    src = tmp_path / "src"
    out = tmp_path / "out"
    src.mkdir()
    out.mkdir()
    return str(src), str(out)


def write(root: str, rel: str, data: bytes) -> str:
    full = os.path.join(root, rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    return full


@pytest.fixture
def basic_site(dirs):
    src, _out = dirs
    write(src, "templates/page.html",
          b"<html>{{include:partials/h.html}}<body>{{content}}</body></html>")
    write(src, "templates/partials/h.html", b"<head>H</head>")
    write(src, "content/index.md", b"# Home\n\nhello")
    write(src, "content/a.md", b"# A page\n\nbody-A-v1")
    write(src, "static/app.css", b"body{}")
    return dirs


@pytest.fixture
def make_engine(dirs):
    def _make(**config_kw):
        src, out = dirs
        kw = dict(concurrency=4, page_timeout=5.0, batch_size=100,
                  full_rebuild_threshold=0.5, keep_releases=0)
        kw.update(config_kw)
        return Engine(src, out, Config(**kw))

    return _make
