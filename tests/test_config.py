"""Configuration validation and automatic full-rebuild fallback."""

import pytest

from isg.config import Config, validate_config


@pytest.mark.parametrize(
    "kwargs",
    [
        {"concurrency": 0},
        {"page_timeout": 0},
        {"batch_size": 0},
        {"full_rebuild_threshold": 0},
        {"full_rebuild_threshold": 1.5},
        {"keep_releases": -1},
    ],
)
def test_invalid_config_rejected(kwargs):
    assert validate_config(Config(**kwargs))


def test_default_config_is_valid():
    assert validate_config(Config()) == []


def test_engine_rejects_invalid_config(dirs):
    from isg.engine import Engine

    src, out = dirs
    with pytest.raises(ValueError):
        Engine(src, out, Config(concurrency=0))


def test_batch_size_overflow_falls_back_to_full_rebuild(make_engine, dirs):
    from tests.conftest import write
    from isg.model import Edit, Op

    src, _out = dirs
    write(src, "templates/page.html", b"<html>{{content}}</html>")
    for i in range(5):
        write(src, f"content/p{i}.md", f"# p{i}".encode())
    engine = make_engine(full_rebuild_threshold=1.0, batch_size=2)
    result = engine.start()
    assert result.success
    # Touching more distinct pages than batch_size forces a full rebuild.
    result = engine.submit_edits([
        Edit("content/p0.md", Op.UPSERT, b"# p0 changed"),
        Edit("content/p1.md", Op.UPSERT, b"# p1 changed"),
        Edit("content/p2.md", Op.UPSERT, b"# p2 changed"),
    ])
    assert result.success
    assert engine.last_plan_reason  # fallback recorded
    assert set(result.ok) == {"p0", "p1", "p2", "p3", "p4"}
