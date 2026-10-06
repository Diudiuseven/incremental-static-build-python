"""Staged release assembly, atomic symlink swap, crash recovery and GC.

On-disk layout::

    output_root/
      releases/
        <release_id>/...complete immutable site files.../manifest.json
        .staging-<release_id>/...writable build in progress...
      current -> releases/<release_id>     (relative symlink)

Readers always follow ``current`` to a *complete* release: a build mutates a
private staging directory, the fully populated directory is renamed into
place, and only then is the symlink atomically swapped. At no point can a
half-built tree be reached through ``current``.
"""

from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime, timezone

from .fsutil import atomic_write, fsync_dir
from .manifest import MANIFEST_NAME, Manifest

CURRENT_LINK = "current"
TMP_LINK = "current.tmp"
RELEASES_DIR = "releases"
STAGING_PREFIX = ".staging-"


class PublishError(Exception):
    """Raised when a staged release cannot be published."""


def new_release_id() -> str:
    """Monotonic, lexicographically sortable timestamp-based release id."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    return f"{ts}-{uuid.uuid4().hex[:8]}"


class ReleaseManager:
    def __init__(self, output_root: str, keep_releases: int = 0):
        self.output_root = os.path.abspath(output_root)
        self.keep_releases = max(0, keep_releases)
        self.releases_dir = os.path.join(self.output_root, RELEASES_DIR)
        self.current_link = os.path.join(self.output_root, CURRENT_LINK)
        self.tmp_link = os.path.join(self.output_root, TMP_LINK)
        os.makedirs(self.releases_dir, exist_ok=True)

    # ----- introspection -------------------------------------------------

    def current_release_dir(self) -> str | None:
        if not os.path.islink(self.current_link):
            return None
        target = os.path.realpath(self.current_link)
        return target if os.path.isdir(target) else None

    def current_release_id(self) -> str | None:
        path = self.current_release_dir()
        return os.path.basename(path) if path else None

    def load_current_manifest(self) -> Manifest | None:
        release_dir = self.current_release_dir()
        if release_dir is None:
            return None
        manifest_path = os.path.join(release_dir, MANIFEST_NAME)
        if not os.path.isfile(manifest_path):
            return None
        with open(manifest_path, encoding="utf-8") as fh:
            return Manifest.from_json(fh.read())

    def list_releases(self) -> list[tuple[str, bool, bool]]:
        """Return [(release_id, complete, referenced)] sorted oldest first."""
        rows: list[tuple[str, bool, bool]] = []
        current_id = self.current_release_id()
        for name in sorted(os.listdir(self.releases_dir)):
            if name.startswith(STAGING_PREFIX) or name.startswith("."):
                continue
            full = os.path.join(self.releases_dir, name)
            if not os.path.isdir(full) or os.path.islink(full):
                continue
            complete = os.path.isfile(os.path.join(full, MANIFEST_NAME))
            rows.append((name, complete, name == current_id))
        return rows

    # ----- staging -------------------------------------------------------

    def stage(self, release_id: str | None = None) -> tuple[str, str]:
        """Create a fresh empty staging directory; return (release_id, path)."""
        release_id = release_id or new_release_id()
        path = os.path.join(self.releases_dir, f"{STAGING_PREFIX}{release_id}")
        os.makedirs(path, exist_ok=False)
        return release_id, path

    def staging_dir(self, release_id: str) -> str:
        return os.path.join(self.releases_dir, f"{STAGING_PREFIX}{release_id}")

    def commit_manifest(self, staging_dir: str, manifest: Manifest) -> None:
        """Write the manifest as the final step of assembling a release."""
        manifest.release_id = os.path.basename(staging_dir)[len(STAGING_PREFIX):]
        atomic_write(os.path.join(staging_dir, MANIFEST_NAME),
                     manifest.to_json().encode("utf-8"))

    # ----- publish / swap ------------------------------------------------

    def publish(self, staging_dir: str) -> str:
        """Atomically publish a fully populated staging directory.

        Returns the new release directory path. Fails without touching
        ``current`` if staging is incomplete or the target name exists.
        """
        if not os.path.isfile(os.path.join(staging_dir, MANIFEST_NAME)):
            raise PublishError("refusing to publish staging dir without manifest")
        release_id = os.path.basename(staging_dir)[len(STAGING_PREFIX):]
        final_dir = os.path.join(self.releases_dir, release_id)
        if os.path.exists(final_dir) or os.path.islink(final_dir):
            raise PublishError(f"release already exists: {release_id}")

        # Rename the complete tree into place (atomic within one directory).
        os.rename(staging_dir, final_dir)
        fsync_dir(self.releases_dir)

        # Swap the symlink atomically; readers see old or new release only.
        if os.path.lexists(self.tmp_link):
            os.unlink(self.tmp_link)
        os.symlink(os.path.join(RELEASES_DIR, release_id), self.tmp_link)
        os.replace(self.tmp_link, self.current_link)
        fsync_dir(self.output_root)
        return final_dir

    # ----- cleanup / recovery -------------------------------------------

    def _remove_tree(self, path: str) -> bool:
        if not (os.path.abspath(path).startswith(os.path.abspath(self.releases_dir))
                and os.path.abspath(path) != os.path.abspath(self.releases_dir)):
            return False
        shutil.rmtree(path, ignore_errors=True)
        return not os.path.exists(path)

    def remove_staging(self, staging_dir: str) -> None:
        """Discard an aborted staging directory completely."""
        if os.path.abspath(os.path.dirname(staging_dir)) != os.path.abspath(self.releases_dir):
            return
        if os.path.basename(staging_dir).startswith(STAGING_PREFIX):
            shutil.rmtree(staging_dir, ignore_errors=True)

    def recover(self) -> list[str]:
        """Clean leftovers from interrupted builds/processes at startup."""
        removed: list[str] = []
        if os.path.lexists(self.tmp_link):
            os.unlink(self.tmp_link)
            removed.append(TMP_LINK)
        for name in list(os.listdir(self.releases_dir)):
            full = os.path.join(self.releases_dir, name)
            if name.startswith(STAGING_PREFIX):
                shutil.rmtree(full, ignore_errors=True)
                removed.append(name)
                continue
            if os.path.isdir(full) and not os.path.islink(full):
                if not os.path.isfile(os.path.join(full, MANIFEST_NAME)):
                    # Renamed but never finished/published: do not resurrect it.
                    shutil.rmtree(full, ignore_errors=True)
                    removed.append(name)
        # Releases newer than the current one can only be unpublished aborts
        # because release ids are monotonic.
        current_id = self.current_release_id()
        if current_id is not None:
            for name, complete, referenced in self.list_releases():
                if not referenced and complete and name > current_id:
                    if self._remove_tree(os.path.join(self.releases_dir, name)):
                        removed.append(name)
        removed.extend(self.gc())
        fsync_dir(self.releases_dir)
        fsync_dir(self.output_root)
        return removed

    def gc(self) -> list[str]:
        """Delete releases no longer referenced, keeping the newest N old ones."""
        removed: list[str] = []
        current_id = self.current_release_id()
        old: list[str] = []
        for name, complete, referenced in self.list_releases():
            if referenced:
                continue
            if not complete:
                continue
            if current_id and name > current_id:
                continue  # handled by recover()
            old.append(name)
        old.sort()
        for name in old[: max(0, len(old) - self.keep_releases)]:
            if self._remove_tree(os.path.join(self.releases_dir, name)):
                removed.append(name)
        fsync_dir(self.releases_dir)
        return removed
