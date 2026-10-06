"""Manifest of a published release.

The manifest is the single source of truth describing a release: which
routes exist, the content-addressed object backing each artifact, and the
dependency keys of every page. Readers use only the manifest of the
release that ``current`` points at, so they always see one self-consistent
version.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from isg.fsutil import atomic_write_bytes
from isg.model import ReleaseInfo

MANIFEST_NAME = "manifest.json"
CURRENT_LINK = "current"


def release_manifest_path(release_dir: Path) -> Path:
    return Path(release_dir) / MANIFEST_NAME


def info_to_json(info: ReleaseInfo) -> str:
    # Sorting keys makes manifest bytes deterministic for identical
    # releases (same content -> same output -> easy verification).
    return json.dumps(asdict(info), sort_keys=True, indent=2,
                      ensure_ascii=False)


def info_from_json(text: str) -> ReleaseInfo:
    payload = json.loads(text)
    pages: dict[str, tuple[str, str]] = {}
    for route, value in payload.get("pages", {}).items():
        if isinstance(value, list):
            pages[route] = (value[0], value[1])
        else:  # legacy / tolerate plain digest strings
            pages[route] = (value, "")
    return ReleaseInfo(
        release_id=payload["release_id"],
        created_at=payload["created_at"],
        pages=pages,
        deps={k: list(v) for k, v in payload.get("deps", {}).items()},
        objects=dict(payload.get("objects", {})),
        plan_revision=int(payload.get("plan_revision", 0)),
    )


def write_release_manifest(release_dir: Path, info: ReleaseInfo) -> None:
    atomic_write_bytes(release_manifest_path(release_dir),
                       info_to_json(info).encode("utf-8"))


def read_release_manifest(release_dir: Path) -> ReleaseInfo:
    path = release_manifest_path(release_dir)
    return info_from_json(path.read_text(encoding="utf-8"))
