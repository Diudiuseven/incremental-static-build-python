"""Release staging, content-addressed object store and atomic publishing.

On-disk layout under ``output_root``::

    objects/<digest[:2]>/<digest>   immutable, content-addressed artifacts
    releases/<release_id>/           one complete, self-consistent site
    staging/<release_id>/            in-progress build scratch (never served)
    current -> releases/<id>         the single published pointer

Publish protocol
----------------
1. Every artifact is written to the immutable object store first.
2. The whole release tree is materialized in ``staging/<id>`` using
   hardlinks (copy fallback) to objects; the manifest is written last.
3. The completed staging directory is renamed into ``releases/``.
4. ``current`` is switched with an atomic symlink rename.

Therefore readers resolving ``current`` always observe either the previous
release or the new one in full -- never a partial or mixed tree. A crash
before step 4 leaves only stale staging/unreferenced release directories,
which :meth:`ReleaseStore.recover` cleans on startup.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from isg.fsutil import (atomic_symlink, atomic_write_bytes, ensure_clean_dir,
                        fsync_dir, remove_path, sha256_bytes)
from isg.manifest import CURRENT_LINK, read_release_manifest, write_release_manifest
from isg.model import ReleaseInfo
from isg.sources import route_output_relpath


class ReleaseError(Exception):
    pass


class ReleaseStore:
    def __init__(self, output_root: Path, keep_releases: int = 1):
        self.root = Path(output_root)
        self.objects_dir = self.root / "objects"
        self.releases_dir = self.root / "releases"
        self.staging_dir = self.root / "staging"
        self.current_link = self.root / CURRENT_LINK
        self.keep_releases = max(0, keep_releases)
        for d in (self.root, self.objects_dir, self.releases_dir,
                  self.staging_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- object store ----------------------------------------------------
    def put_object(self, data: bytes) -> str:
        """Store ``data`` content-addressed; return its object relpath.

        Objects are immutable: an existing entry is never overwritten, so
        retries and duplicate builds cannot corrupt shared artifacts.
        """
        digest = sha256_bytes(data)
        rel = Path("objects") / digest[:2] / digest
        target = self.root / rel
        if not target.exists():
            atomic_write_bytes(target, data)
        return rel.as_posix()

    def object_path(self, object_rel: str) -> Path:
        return self.root / object_rel

    # -- staging ---------------------------------------------------------
    def new_staging(self, release_id: str) -> Path:
        staging = self.staging_dir / release_id
        if staging.exists():
            ensure_clean_dir(staging)
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def staging_path(self, release_id: str) -> Path:
        return self.staging_dir / release_id

    def commit_staging(self, staging: Path, info: ReleaseInfo) -> Path:
        """Publish a fully prepared staging directory.

        ``info`` already carries every route's object location; artifacts
        are linked from the object store. The method either publishes the
        complete release or raises without touching ``current``.
        """
        release_dir = self.releases_dir / info.release_id
        if release_dir.exists():
            raise ReleaseError(f"release already exists: {info.release_id}")

        # 1. materialize all page artifacts from the object store.
        for route, (_digest, object_rel) in info.pages.items():
            obj = self.object_path(object_rel)
            if not obj.exists():
                raise ReleaseError(f"missing object {object_rel}")
            out = staging / route_output_relpath(route)
            out.parent.mkdir(parents=True, exist_ok=True)
            self._link_or_copy(obj, out)

        # 2. manifest last inside staging; it marks a tree as complete.
        write_release_manifest(staging, info)
        fsync_dir(staging)

        # 3. move the completed tree atomically into releases/.
        os.rename(staging, release_dir)
        fsync_dir(self.releases_dir)

        # 4. switch the published pointer atomically.
        target = Path("releases") / info.release_id
        atomic_symlink(target, self.current_link)
        return release_dir

    @staticmethod
    def _link_or_copy(obj: Path, dst: Path) -> None:
        try:
            os.link(obj, dst)
        except OSError:
            shutil.copyfile(obj, dst)

    def discard_staging(self, staging: Path) -> None:
        remove_path(staging)

    # -- current / recovery ---------------------------------------------
    def current_dir(self) -> Path | None:
        if not self.current_link.is_symlink():
            return None
        resolved = self.current_link.resolve()
        if not resolved.exists():
            return None
        return resolved

    def current(self) -> ReleaseInfo | None:
        release_dir = self.current_dir()
        if release_dir is None:
            return None
        return read_release_manifest(release_dir)

    def recover(self) -> None:
        """Clean interrupted state on startup.

        * removes every staging directory (left by a crashed process);
        * removes release directories lacking a manifest (half-published);
        * repairs a dangling ``current`` pointer using the newest valid
          release, or removes it if none exists.
        """
        if self.staging_dir.exists():
            for child in self.staging_dir.iterdir():
                remove_path(child)

        valid: dict[str, ReleaseInfo] = {}
        for child in list(self.releases_dir.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            manifest = child / "manifest.json"
            if not manifest.exists():
                remove_path(child)
                continue
            try:
                valid[child.name] = read_release_manifest(child)
            except (ValueError, KeyError):
                remove_path(child)

        cur = self.current_dir()
        if cur is None or cur.name not in valid:
            remove_path(self.current_link)
            if valid:
                newest = max(valid, key=lambda rid:
                             (valid[rid].created_at, rid))
                atomic_symlink(Path("releases") / newest, self.current_link)

    # -- pruning ---------------------------------------------------------
    def _live_release_ids(self, current_id: str | None) -> set[str]:
        all_ids = sorted(
            (p.name for p in self.releases_dir.iterdir()
             if p.is_dir() and not p.is_symlink()),
            reverse=True,
        )
        keep = self.keep_releases
        live: set[str] = set()
        if current_id is not None:
            live.add(current_id)
        # Retain the newest ``keep`` releases (current always included).
        for rid in all_ids:
            if len(live) >= keep:
                break
            live.add(rid)
        return live

    def prune(self) -> list[str]:
        """Delete old releases and objects they no longer reference.

        Returns the list of removed release ids. Object files reachable
        from any retained release survive through their hardlinks even if
        removed from the object store, so published pages never break.
        """
        current = self.current()
        current_id = current.release_id if current else None
        live_ids = self._live_release_ids(current_id)

        referenced: set[str] = set()
        for rid in live_ids:
            info = read_release_manifest(self.releases_dir / rid)
            referenced.update(obj.split("/")[-1]
                              for _d, obj in info.pages.values())

        removed: list[str] = []
        for child in list(self.releases_dir.iterdir()):
            if child.is_dir() and not child.is_symlink() \
                    and child.name not in live_ids:
                remove_path(child)
                removed.append(child.name)

        for shard in list(self.objects_dir.iterdir()):
            if not shard.is_dir():
                continue
            for obj_file in list(shard.iterdir()):
                if obj_file.is_file() and obj_file.name not in referenced:
                    # Hardlinks keep the bytes alive inside retained
                    # releases; only the object-store copy is dropped.
                    try:
                        if obj_file.stat().st_nlink <= 1:
                            obj_file.unlink()
                    except FileNotFoundError:
                        pass
            try:
                if not any(shard.iterdir()):
                    shard.rmdir()
            except OSError:
                pass
        return removed
