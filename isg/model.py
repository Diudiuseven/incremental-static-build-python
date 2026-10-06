"""Core data types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ChangeKind(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True)
class ChangeEvent:
    """A single source change notification (idempotent, order independent)."""

    kind: ChangeKind
    path: str
    digest: str = ""


@dataclass
class PageSpec:
    """A page to render: route -> content + template."""

    route: str
    content_path: str
    template: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class BuildResult:
    route: str
    ok: bool
    digest: str = ""
    error: str = ""


@dataclass
class ReleaseInfo:
    release_id: str
    created_at: str
    pages: dict[str, tuple[str, str]]  # route -> (digest, object relpath)
    deps: dict[str, list[str]]    # route -> dependency keys
    objects: dict[str, str]       # object digest -> relative object path
    plan_revision: int
