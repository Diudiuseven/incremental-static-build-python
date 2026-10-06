"""Top level engine: serializes edits and drives incremental builds.

Pipeline per ``submit_edits`` call (all under one lock)::

    1. apply edits durably to the source tree
    2. scan source hashes; diff against previous scan
    3. plan incremental build (or deterministic full-rebuild fallback)
    4. copy current release tree into a fresh staging dir (hardlinks)
    5. render affected pages concurrently, update assets, remove stale files
    6. write new manifest and atomically publish; discard staging on failure

Because planning cross-checks the manifest against the current source tree
rather than trusting edit notifications, repeated edits, out-of-order
arrival and coalesced batches always converge on what a full rebuild would
produce. A failed build is discarded before the symlink swap, so a published
version is never half-updated.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable

from .config import Config, validate_config
from .fsutil import fsync_dir, hardlink_or_copy, safe_join
from .manifest import MANIFEST_NAME, Manifest
from .model import BuildOutcome, Edit
from .planner import (
    _fingerprint,
    current_page_keys,
    is_asset_source,
    plan_build,
)
from .queue import run_pages
from .release import ReleaseManager
from .render import page_key_to_output, render_page
from .sources import SourceStore


class Engine:
    def __init__(self, source_root: str, output_root: str,
                 config: Config | None = None):
        config = config or Config()
        errors = validate_config(config)
        if errors:
            raise ValueError("invalid config: " + "; ".join(errors))
        self.config = config
        self.sources = SourceStore(source_root)
        self.releases = ReleaseManager(output_root, config.keep_releases)
        self._lock = threading.RLock()
        self._last_scan: dict[str, str] | None = None
        self.last_build: BuildOutcome | None = None
        self.last_plan_reason: str | None = None
        self.last_release_id: str | None = None

    # ----- lifecycle -----------------------------------------------------

    def start(self) -> BuildOutcome:
        """Recover interrupted releases and ensure at least one published site."""
        with self._lock:
            self.releases.recover()
            self._last_scan = self.sources.scan()
            if self.releases.load_current_manifest() is None:
                return self._build_and_publish(
                    changed=frozenset(self._last_scan), deleted=frozenset(),
                    force_full=True, reason="initial build")
            return BuildOutcome()

    def site_path(self) -> str | None:
        return self.releases.current_release_dir()

    # ----- edits ---------------------------------------------------------

    def submit_edits(self, edits: Iterable[Edit]) -> BuildOutcome:
        """Apply edits durably, then build and publish everything affected."""
        with self._lock:
            before = self._last_scan if self._last_scan is not None \
                else self.sources.scan()
            edits = list(edits)
            self.sources.apply_edits(edits)
            after = self.sources.scan()
            changed, deleted = _diff_scans(before, after)
            self._last_scan = after
            if not changed and not deleted:
                self.last_build = BuildOutcome()
                self.last_plan_reason = "no effective changes"
                return self.last_build
            return self._build_and_publish(
                changed=frozenset(changed), deleted=frozenset(deleted))

    def rebuild_full(self) -> BuildOutcome:
        """Force a full rebuild and publish it."""
        with self._lock:
            after = self.sources.scan()
            self._last_scan = after
            return self._build_and_publish(
                changed=frozenset(after), deleted=frozenset(),
                force_full=True, reason="manual full rebuild")

    # ----- build pipeline ------------------------------------------------

    def _build_and_publish(self, *, changed: frozenset[str],
                           deleted: frozenset[str],
                           force_full: bool = False,
                           reason: str | None = None) -> BuildOutcome:
        current_hashes = self._last_scan or {}
        manifest = self.releases.load_current_manifest()

        if force_full or manifest is None:
            plan_full = True
            dirty = set(current_page_keys(current_hashes))
            deleted_pages = frozenset(manifest.pages) if manifest else frozenset()
            changed_assets = frozenset(
                p for p in current_hashes if is_asset_source(p))
            deleted_assets = frozenset(manifest.assets) if manifest else frozenset()
            plan_reason = reason or ("no previous manifest" if manifest is None
                                     else "forced full rebuild")
        else:
            plan = plan_build(
                manifest=manifest, current_hashes=current_hashes,
                changed=changed, deleted=deleted,
                threshold=self.config.full_rebuild_threshold,
                batch_size=self.config.batch_size)
            plan_full = plan.full
            plan_reason = plan.reason
            if plan_full:
                dirty = set(current_page_keys(current_hashes))
                deleted_pages = frozenset(manifest.pages)
                changed_assets = frozenset(
                    p for p in current_hashes if is_asset_source(p))
                deleted_assets = frozenset(manifest.assets)
            else:
                dirty = set(plan.dirty_pages)
                deleted_pages = plan.deleted_pages
                changed_assets = plan.changed_assets
                deleted_assets = plan.deleted_assets
        self.last_plan_reason = plan_reason

        release_id, staging = self.releases.stage()
        outcome = BuildOutcome()
        try:
            self._seed_from_previous(staging, manifest)
            new_manifest = manifest.clone() if manifest else Manifest(release_id)

            self._apply_assets(staging, changed_assets, deleted_assets,
                               new_manifest, current_hashes)
            outcome = self._render_pages(sorted(dirty), new_manifest,
                                         staging, current_hashes)
            self._remove_deleted_pages(staging, deleted_pages, new_manifest)

            # Pages present in source but missing from the manifest would be
            # detected by the planner; this guards the full path as well.
            missing = set(current_page_keys(current_hashes)) - set(new_manifest.pages)
            if missing and not outcome.failed and not outcome.timed_out:
                extra = self._render_pages(sorted(missing), new_manifest,
                                           staging, current_hashes)
                outcome = BuildOutcome(
                    ok={**outcome.ok, **extra.ok},
                    failed={**outcome.failed, **extra.failed},
                    timed_out=[*outcome.timed_out, *extra.timed_out])

            if not outcome.success:
                # Never publish: the previous release stays current and the
                # staging directory is discarded without partial state.
                self.releases.remove_staging(staging)
                self.last_build = outcome
                return outcome

            self.releases.commit_manifest(staging, new_manifest)
            self.releases.publish(staging)
        except BaseException:
            self.releases.remove_staging(staging)
            raise

        self.last_release_id = release_id
        self.releases.gc()
        fsync_dir(self.releases.output_root)
        self.last_build = outcome
        return self.last_build

    def _seed_from_previous(self, staging: str, manifest: Manifest | None) -> None:
        previous = self.releases.current_release_dir()
        if previous is None:
            return
        # Copy the complete previous tree so unchanged files are reused via
        # hard links and the staging dir is itself a full, self-contained site.
        for name in os.listdir(previous):
            if name == MANIFEST_NAME:
                continue
            src = os.path.join(previous, name)
            dst = os.path.join(staging, name)
            if os.path.isdir(src) and not os.path.islink(src):
                for root, _dirs, files in os.walk(src):
                    for fname in files:
                        fsrc = os.path.join(root, fname)
                        rel = os.path.relpath(fsrc, previous)
                        hardlink_or_copy(fsrc, os.path.join(staging, rel))
            else:
                hardlink_or_copy(src, dst)

    def _render_pages(self, page_keys: list[str], manifest: Manifest,
                      staging: str, current_hashes: dict[str, str]) -> BuildOutcome:
        snapshot = self.sources.snapshot()

        def render_one(key: str) -> None:
            source_path = f"content/index.md" if key == "" else f"content/{key}.md"
            result = render_page(source_path, snapshot)
            output_rel = page_key_to_output(key)
            target = safe_join(staging, output_rel)
            if os.path.dirname(target):
                os.makedirs(os.path.dirname(target), exist_ok=True)
            # Write to a fresh temp name then replace, so a retried render
            # never leaves a partially written output file.
            tmp = target + ".part"
            with open(tmp, "wb") as fh:
                fh.write(result.html)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            manifest.pages[key] = _fingerprint(
                {d: current_hashes.get(d) for d in result.dependencies})
            manifest.deps[key] = sorted(result.dependencies)

        return run_pages(page_keys, render_one,
                         concurrency=self.config.concurrency,
                         timeout=self.config.page_timeout)

    def _apply_assets(self, staging: str, changed: frozenset[str],
                      deleted: frozenset[str], manifest: Manifest,
                      current_hashes: dict[str, str]) -> None:
        for path in changed:
            src = self.sources._full(path)  # noqa: SLF001
            rel = path[len("static/"):]
            dst = safe_join(staging, rel)
            hardlink_or_copy(src, dst)
            manifest.assets[path] = current_hashes[path]
        for path in deleted:
            rel = path[len("static/"):]
            target = safe_join(staging, rel)
            try:
                os.remove(target)
            except FileNotFoundError:
                pass
            manifest.assets.pop(path, None)

    def _remove_deleted_pages(self, staging: str, deleted_pages: frozenset[str],
                              manifest: Manifest) -> None:
        for key in deleted_pages:
            output_rel = page_key_to_output(key)
            target = safe_join(staging, output_rel)
            try:
                os.remove(target)
            except FileNotFoundError:
                pass
            manifest.pages.pop(key, None)
            manifest.deps.pop(key, None)
            # Remove now-empty leaf directory (e.g. guides/a/ for page guides/a).
            parent = os.path.dirname(target)
            staging_abs = os.path.abspath(staging)
            while parent.startswith(staging_abs) and parent != staging_abs:
                try:
                    os.rmdir(parent)
                except OSError:
                    break
                parent = os.path.dirname(parent)


def _diff_scans(before: dict[str, str], after: dict[str, str]) -> tuple[set[str], set[str]]:
    changed = {p for p, h in after.items() if before.get(p) != h}
    deleted = set(before) - set(after)
    return changed, deleted
