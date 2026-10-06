"""Configuration validation."""

import pytest

from isg.config import Config
from isg.queue import CoalescingQueue


def test_defaults_are_valid():
    Config().validate()


@pytest.mark.parametrize("kwargs", [
    {"concurrency": 0},
    {"page_timeout": 0},
    {"page_timeout": -1},
    {"batch_size": 0},
    {"full_rebuild_threshold": 1.1},
    {"full_rebuild_threshold": -0.1},
    {"keep_releases": -1},
    {"batch_idle_seconds": -0.01},
])
def test_invalid_config_rejected(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs).validate()


def test_batch_size_caps_drain():
    q = CoalescingQueue(batch_size=3)
    from isg.model import ChangeEvent, ChangeKind
    for i in range(10):
        q.put(ChangeEvent(ChangeKind.UPSERT, f"p{i}"))
    assert len(q.drain()) == 3
    assert len(q.drain()) == 3
    assert len(q.drain()) == 3
    assert len(q.drain()) == 1


def test_threshold_boundaries_valid():
    Config(full_rebuild_threshold=0.0).validate()
    Config(full_rebuild_threshold=1.0).validate()
