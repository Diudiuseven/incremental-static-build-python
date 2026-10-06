"""Build configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    #: Max pages rendered concurrently.
    concurrency: int = 4
    #: Per-page render/build timeout in seconds.
    page_timeout: float = 10.0
    #: Max change events coalesced into one incremental batch.
    batch_size: int = 100
    #: Fraction in [0, 1] of existing pages affected by a batch at/above
    #: which the engine falls back to a full rebuild.
    full_rebuild_threshold: float = 0.5
    #: Number of historical release trees to retain for rollback/inspection.
    #: Unreferenced objects are pruned regardless once older releases go.
    keep_releases: int = 1
    #: Idle seconds the background builder waits for the change queue to
    #: settle before running a cycle (coalescing bursty editors).
    batch_idle_seconds: float = 0.05

    def validate(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.page_timeout <= 0:
            raise ValueError("page_timeout must be > 0")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if not 0.0 <= self.full_rebuild_threshold <= 1.0:
            raise ValueError("full_rebuild_threshold must be within [0, 1]")
        if self.keep_releases < 0:
            raise ValueError("keep_releases must be >= 0")
        if self.batch_idle_seconds < 0:
            raise ValueError("batch_idle_seconds must be >= 0")
