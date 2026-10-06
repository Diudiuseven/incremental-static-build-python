"""Source tree scanning, hashing and atomic source edits."""

from __future__ import annotations

import os
from collections.abc import Iterable

from .fsutil import (
    atomic_write,
    fsync_dir,
    hash_file,
    iter_files,
    normalize_rel,
    remove_file,
    safe_join,
)
from .model import Edit, Op

SUBDIRS = ("content", "templates", "static")


class SourceStore:
    """Owns the source directory and tracks current file hashes."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        for sub in SUBDIRS:
            os.makedirs(os.path.join(self.root, sub), exist_ok=True)

    def _full(self, rel: str) -> str:
        rel = normalize_rel(rel)
        full = safe_join(self.root, rel)
        if os.path.commonpath([full, self.root]) != self.root:
            raise ValueError(f"path escapes source root: {rel!r}")
        return full

    def scan(self) -> dict[str, str]:
        """Return {site-relative posix path: content hash} for source files."""
        result: dict[str, str] = {}
        for sub in SUBDIRS:
            base = os.path.join(self.root, sub)
            for rel in iter_files(base):
                path = f"{sub}/{rel}"
                result[path] = hash_file(os.path.join(base, rel))
        return result

    def snapshot(self) -> dict[str, bytes]:
        """Return {path: raw content} of every source file."""
        return {path: self.read(path) or b"" for path in self.scan()}

    def read(self, path: str) -> bytes | None:
        """Read one source file; None when it does not exist."""
        full = self._full(path)
        try:
            with open(full, "rb") as fh:
                return fh.read()
        except FileNotFoundError:
            return None

    def apply_edits(self, edits: Iterable[Edit]) -> None:
        """Apply a batch of upserts/deletes durably and in given order.

        Individual file writes are atomic; callers must hold the engine's
        edit lock so concurrent batches cannot interleave on disk.
        Re-applying the same batch is safe: upserts are idempotent and
        deleting a missing file is treated as success.
        """
        for edit in edits:
            rel = normalize_rel(edit.path)
            full = self._full(rel)
            if edit.op == Op.UPSERT:
                atomic_write(full, b"" if edit.content is None else edit.content)
            elif edit.op == Op.DELETE:
                if remove_file(full):
                    parent = os.path.dirname(full)
                    while parent.startswith(self.root) and parent != self.root:
                        try:
                            os.rmdir(parent)
                        except OSError:
                            break
                        parent = os.path.dirname(parent)
            else:  # pragma: no cover - exhaustive enum
                raise ValueError(f"unknown op: {edit.op!r}")
        fsync_dir(self.root)
