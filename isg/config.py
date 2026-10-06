"""Build engine configuration."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    # Max pages rendered concurrently within one build.
    concurrency: int = 4
    # Per-page render timeout in seconds.
    page_timeout: float = 10.0
    # Max number of dirty pages merged into one incremental build.
    # Larger change sets trigger an automatic full rebuild.
    batch_size: int = 100
    # When a build plan touches at least this fraction (0..1) of all pages,
    # fall back to a full rebuild.
    full_rebuild_threshold: float = 0.5
    # Number of old release snapshots retained after publishing.
    keep_releases: int = 0


def validate_config(config: Config) -> list[str]:
    """Return human readable error messages; an empty list means valid."""
    errors: list[str] = []
    if config.concurrency < 1:
        errors.append("concurrency must be >= 1")
    if config.page_timeout <= 0:
        errors.append("page_timeout must be > 0")
    if config.batch_size < 1:
        errors.append("batch_size must be >= 1")
    if not 0.0 < config.full_rebuild_threshold <= 1.0:
        errors.append("full_rebuild_threshold must be within (0, 1]")
    if config.keep_releases < 0:
        errors.append("keep_releases must be >= 0")
    return errors
