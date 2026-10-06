# Architecture & consistency design

This document records the internal design of `isg` and the exact semantics
that make incremental builds safe.

## Components

| Module        | Responsibility |
|---------------|----------------|
| `sources.py`  | Ordered source-tree scan; frontmatter; content-path → route mapping. |
| `render.py`   | Deterministic template renderer; reports transitive `@include` deps. |
| `planner.py`  | Bidirectional route ↔ dependency graph; turns change events into a `Plan`. |
| `queue.py`    | Coalescing, order-independent event queue; bounded timeout executor. |
| `release.py`  | Content-addressed object store; staging; atomic symlink publish; prune/recover. |
| `manifest.py` | Per-release manifest (routes, digests, objects, dependency keys). |
| `engine.py`   | Orchestration: snapshot → plan → render → stage → publish → prune. |
| `api.py`      | Editing endpoints + reads served strictly from the current release. |

## Build cycle

```
drain coalesced events (under queue lock)
        │
read ONE source snapshot ──► analyze pages (route, template, data)
        │
plan:
   • first publish / force / no current  → FULL plan
   • otherwise Planner.plan(events, snapshot, graph, threshold)
        │  affected fraction >= threshold → full fallback
        ▼
staging/<id>:
   • carry unchanged pages forward from the previous manifest (incremental)
   • render affected pages concurrently (per-page timeout)
   • put each artifact in the immutable object store
   • any failure → discard staging, DO NOT touch current
        │
write manifest LAST inside staging
rename staging → releases/<id>            (atomic within releases/)
current  = releases/<id>                  (atomic symlink rename)
prune old releases + unreferenced objects
```

The build lock (`Engine._lock`) serializes cycles. The background builder
and synchronous `rebuild_now()` go through the same path, so there is ever
at most one publish in progress.

## Why repeated / out-of-order edits converge

The queue de-duplicates by path and drains in sorted order, but correctness
does not rely on that alone. A plan is computed by **reconciling the change
set against the current source snapshot and the persisted dependency
graph**:

* content that exists in the snapshot for an affected route → build;
* content whose route is in the graph but absent from the snapshot →
  delete;
* template/partial/data keys → fan out through the reverse index.

Since the final snapshot is what gets rendered, any sequence of identical
repeats or reorderings that ends in the same on-disk state yields the same
set of pages and bytes as a full rebuild.

## Atomic publication protocol

1. Artifacts are stored at `objects/<sha[:2]>/<sha>`; objects are immutable
   and never overwritten (retries share the same content-addressed bytes).
2. A release tree is assembled under `staging/<id>` using hardlinks (copy on
   filesystems without hardlink support). Pages are linked first.
3. `manifest.json` is written last (atomic temp-file + rename); its presence
   marks the tree complete.
4. The complete staging directory is `rename()`-d into `releases/`.
5. `current` is replaced by an atomic symlink rename (`os.replace`).

Readers resolve a single symlink and then read artifacts only from that
release directory, so the switch is linearizable: every read is entirely
old or entirely new.

## Crash / failure recovery

* **Page failure or timeout:** the cycle raises before commit;
  `staging/<id>` is removed and `current` remains the prior release. The
  dependency graph and on-disk release are unchanged, so the next cycle
  retries without double-applying or skipping anything.
* **Process crash during staging:** on startup `ReleaseStore.recover()`
  deletes every `staging/*` directory and any `releases/*` directory lacking
  a manifest, then repairs a dangling `current` pointer using the newest
  valid release (or removes it if none exists).
* **Timeouts:** each render gets an independent deadline measured from
  submission; a slow page occupies one worker slot while other pages finish,
  and is reported as a failure instead of blocking the build. Workers are
  daemon threads and `Engine.stop()` joins them with a bounded grace period,
  so an uncooperative task cannot block shutdown.

## Garbage collection

`prune()` keeps the current release plus the newest `keep_releases`
releases, removes older release directories, and deletes object-store files
whose digest is not referenced by any retained release. Because release
artifacts are hardlinks into the object store, removing an object-store copy
does not affect bytes still linked by a live release; bytes with zero links
are physically reclaimed by the filesystem.

## Configuration notes

`full_rebuild_threshold` is inclusive (`touched/total >= threshold` ⇒ full
rebuild). Both branches use identical rendering and staging code; the full
branch simply includes every route and computes deletes as
`graph_routes − snapshot_routes`, which is why the two paths produce
byte-identical artifacts.
