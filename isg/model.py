"""Core data types shared across modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Op(str, Enum):
    UPSERT = "upsert"
    DELETE = "delete"


@dataclass(frozen=True)
class Edit:
    """A single source change: create/update or delete a source file."""

    path: str
    op: Op
    content: bytes | None = None


class PageStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class BuildOutcome:
    """Result of a build: pages rendered ok, failed or timed out."""

    ok: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    timed_out: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.failed and not self.timed_out

    def merge(self, other: "BuildOutcome") -> "BuildOutcome":
        ok = {**self.ok, **other.ok}
        failed = {**self.failed, **other.failed}
        timed_out = [*self.timed_out,
                     *(p for p in other.timed_out if p not in self.timed_out)]
        return BuildOutcome(ok=ok, failed=failed, timed_out=timed_out)
