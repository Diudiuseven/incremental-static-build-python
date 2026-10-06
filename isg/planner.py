"""Decide which pages to (re)build, or when to fall back to a full rebuild.

The planner is a pure function of (previous manifest, current source hashes,
changed/deleted source paths). Repeated changes, out-of-order arrivals and
batched edits all converge on the same plan: it never trusts "what changed"
alone and cross-checks every manifest entry against the current source tree.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .render import page_source_to_key


def is_page_source(path: str) -> bool:
    return path.startswith("content/") and path.endswith(".md")


def is_asset_source(path: str) -> bool:
    return path.startswith("static/")


def page_key_of(path: str) -> str:
    return page_source_to_key(path)


def current_page_keys(current_hashes: dict[str, str]) -> list[str]:
    keys = {page_key_of(p) for p in current_hashes if is_page_source(p)}
    return sorted(keys)


@dataclass(frozen=True)
class Plan:
    full: bool
    changed_inputs: frozenset[str] = field(default_factory=frozenset)
    dirty_pages: frozenset[str] = field(default_factory=frozenset)
    deleted_pages: frozenset[str] = field(default_factory=frozenset)
    changed_assets: frozenset[str] = field(default_factory=frozenset)
    deleted_assets: frozenset[str] = field(default_factory=frozenset)
    reason: str = ""


def plan_build(
    *,
    manifest,
    current_hashes: dict[str, str],
    changed: frozenset[str],
    deleted: frozenset[str],
    threshold: float,
    batch_size: int,
) -> Plan:
    """Compute the build plan; ``manifest`` may be None for the first build."""
    manifest = manifest
    pages_now = set(current_page_keys(current_hashes))
    page_to_source: dict[str, str] = {}
    for path in current_hashes:
        if is_page_source(path):
            page_to_source[page_key_of(path)] = path
    assets_now = {p for p in current_hashes if is_asset_source(p)}

    if manifest is None:
        return Plan(full=True, reason="no previous manifest")

    known_pages = set(manifest.pages)
    known_assets = set(manifest.assets)

    # 1) Pages whose own source changed, plus pages depending on any changed
    #    or deleted source (templates, snippets, data, assets are inputs too).
    dirty: set[str] = set()
    affected_inputs = set(changed) | set(deleted)
    for page, dep_list in manifest.deps.items():
        if page in pages_now and affected_inputs.intersection(dep_list):
            dirty.add(page)
    for path in changed | deleted:
        if is_page_source(path):
            key = page_key_of(path)
            if key in pages_now:
                dirty.add(key)

    # 2) Cross-check: a previously published page is stale whenever its
    #    recorded source dependencies differ from the current tree. This
    #    makes out-of-order / repeated / collapsed edits converge.
    for key in sorted(known_pages & pages_now):
        dep_hashes = {
            dep: current_hashes.get(dep)
            for dep in manifest.deps.get(key, ())
        }
        own = page_to_source[key]
        recorded_own = manifest.pages.get(key)
        # Missing dependency or the page's own source unknown => must rebuild.
        if any(h is None for h in dep_hashes.values()):
            dirty.add(key)
        elif recorded_own is None:
            dirty.add(key)
        else:
            fingerprint = _fingerprint(dep_hashes)
            if fingerprint != recorded_own:
                dirty.add(key)

    # 3) Pages whose source was deleted, and new pages never published.
    deleted_pages = frozenset(known_pages - pages_now)
    dirty |= pages_now - known_pages
    dirty &= pages_now

    changed_assets = frozenset(
        a for a in assets_now
        if a not in known_assets
        or manifest.assets.get(a) != current_hashes.get(a)
    )
    deleted_assets = frozenset(known_assets - assets_now)

    total = len(pages_now)
    ratio = (len(dirty) / total) if total else 0.0
    change_count = len(changed) + len(deleted)
    if len(dirty) > batch_size:
        return Plan(full=True, changed_inputs=frozenset(affected_inputs),
                    deleted_pages=deleted_pages, reason="dirty pages exceed batch_size")
    if total and ratio > threshold:
        return Plan(full=True, changed_inputs=frozenset(affected_inputs),
                    deleted_pages=deleted_pages,
                    reason=f"dirty ratio {ratio:.2f} > threshold {threshold:.2f}")
    if change_count > batch_size:
        return Plan(full=True, changed_inputs=frozenset(affected_inputs),
                    deleted_pages=deleted_pages,
                    reason="edit burst exceeds batch_size")

    return Plan(
        full=False,
        changed_inputs=frozenset(affected_inputs),
        dirty_pages=frozenset(dirty),
        deleted_pages=deleted_pages,
        changed_assets=changed_assets,
        deleted_assets=deleted_assets,
    )


def _fingerprint(dep_hashes: dict[str, str | None]) -> str:
    import hashlib

    h = hashlib.blake2b(digest_size=16)
    for dep in sorted(dep_hashes):
        h.update(dep.encode())
        h.update(b"=")
        h.update((dep_hashes[dep] or "").encode())
        h.update(b";")
    return h.hexdigest()
