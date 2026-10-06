# Incremental Static Generator (`isg`)

A small, dependency-light static site engine that rebuilds **only the pages
affected by a source change** while always serving a **complete,
self-consistent release**. Readers never see half-rendered pages, mixed
versions, or downtime during publication.

It is organized as a library (`isg/`) plus an optional FastAPI service
(`main.py`, `isg/api.py`) that accepts change notifications and serves the
currently published release.

## Why

A naive generator rebuilds the whole site for every edit and writes output
files in place: publish latency grows with site size, and a reader that
arrives mid-publish can observe a mix of old and new pages (or a missing
page). `isg` solves both problems:

* **Incremental builds with dependency tracking** — a page is rebuilt only
  when one of its dependencies (its content, its template, an included
  partial, or a declared data file) changes.
* **Atomic, immutable releases** — every build is materialized in full in a
  private staging directory and published with a single atomic symlink
  switch. The old release keeps serving throughout the build.

## Source layout

```
site/
  content/            # *.md pages; index.md -> "/", foo/bar.md -> "/foo/bar"
  templates/          # *.html templates; "_"-prefixed names are partials
  data/               # *.json data files a page may declare as dependencies
```

A content file may start with a `---` frontmatter block:

```markdown
---
title: My page
template: page.html     # optional, defaults to page.html
data: posts.json        # optional; JSON array or comma/space list
---
# Markdown body
```

Template syntax (intentionally minimal):

* `@include('_partial.html')` — include a partial (transitively tracked);
* `{{ value }}` — HTML-escaped variable, `{{{ value }}}` — raw HTML;
* `@foreach(item in items) ... @end` — loops;
* markdown subset: `#`/`##` headings, paragraphs, `- ` lists, `**bold**`,
  `[text](url)`.

## Quick start

```bash
uv sync
# Build + serve the bundled example:
ISG_SOURCE_DIR=site_example ISG_OUTPUT_DIR=.build python main.py
```

Notify the engine of a source change (files are read from disk at build
time; the event only marks the path changed):

```bash
curl -X POST "localhost:8000/events?path=content/index.md&wait=true"
curl -X POST "localhost:8000/events/batch?wait=true" \
  -H 'content-type: application/json' \
  -d '[{"kind":"upsert","path":"content/a.md"},
       {"kind":"delete","path":"content/b.md"}]'
curl -X POST "localhost:8000/rebuild"          # drain + build now
curl -X POST "localhost:8000/rebuild?full=true"
curl       "localhost:8000/__site/manifest.json"
```

## On-disk release layout

```
<output>/
  objects/<sha[:2]>/<sha>     immutable, content-addressed artifacts
  releases/<release-id>/       one complete site (hardlinks into objects)
  staging/<release-id>/        in-progress build; never served
  current -> releases/<id>     the single published pointer
```

All page bytes are written to the immutable object store first, a whole
release tree is staged, the manifest is written last, the tree is renamed
into `releases/`, and `current` is switched atomically. A crash before the
switch leaves only staging/unreferenced directories, cleaned by
`ReleaseStore.recover()` on the next startup.

## Consistency guarantees

1. **Incremental == full rebuild.** A build renders from one fresh,
   point-in-time snapshot of the source tree. The plan is derived from the
   snapshot and the recorded dependency graph, not from event ordering.
   Repeated, reordered, or coalesced events therefore converge on exactly
   the artifacts a clean full rebuild produces. Tests assert byte-for-byte
   equality of the incremental and full paths.
2. **Dependency fan-out.** When a template, partial, or data file changes,
   every page that depends on it is marked stale and rebuilt.
3. **Atomic publication.** Readers resolve through one `current` symlink and
   one release manifest; they observe either the previous release or the new
   one in full — never a partial page, mixed versions, or unavailability.
4. **Failure isolation & recovery.** A page render error/timeout fails the
   cycle; staging is discarded and `current` is untouched. A later cycle
   retries from the same snapshot (no double-apply, no skipped artifacts, no
   half-written directories).
5. **Deletion & garbage collection.** Deleted source pages disappear from
   the new release's manifest and tree. Old releases beyond
   `keep_releases` are pruned, and object-store bytes not referenced by any
   retained release are removed (hardlinks keep live pages intact).
6. **Deterministic concurrent edits.** All submissions funnel through one
   coalescing, order-independent queue and a single build lock.
7. **Slow-task isolation.** Page builds run on a bounded pool with an
   independent per-page deadline; one slow/hung page cannot block unrelated
   pages from completing.

When the fraction of pages affected by a batch reaches
`full_rebuild_threshold`, the engine transparently performs a full rebuild
through the same render path, guaranteeing identical output.

## Configuration

| Option (env var)                  | Default | Meaning |
|-----------------------------------|---------|---------|
| `concurrency` (`ISG_CONCURRENCY`) | `4`     | Max parallel page renders. |
| `page_timeout` (`ISG_PAGE_TIMEOUT`) | `10`  | Per-page build timeout (seconds). |
| `batch_size` (`ISG_BATCH_SIZE`)   | `100`   | Max change events coalesced per batch. |
| `full_rebuild_threshold` (`ISG_FULL_REBUILD_THRESHOLD`) | `0.5` | Affected-page fraction (0–1) at/above which a full rebuild is used. |
| `keep_releases` (`ISG_KEEP_RELEASES`) | `1`  | Historical release trees retained; `0` prunes the previous release immediately. |
| `batch_idle_seconds` (`ISG_BATCH_IDLE`) | `0.05` | Settling window that coalesces a burst of edits. |
| `ISG_SOURCE_DIR` / `ISG_OUTPUT_DIR` | `site` / `.build` | Source and output roots. |

## Programmatic use

```python
from isg import Config, Engine
from isg.model import ChangeEvent, ChangeKind

engine = Engine("site", ".build", Config(concurrency=8))
engine.start()
engine.submit(ChangeEvent(ChangeKind.UPSERT, "content/index.md"))
engine.wait_idle(timeout=10)
print(engine.read_current_file("/")[:80])
engine.stop()
```

## Tests

```bash
python -m pytest tests/ -q
```

Covers repeated/out-of-order edits, concurrent editors, dependency fan-out
(template/partial/data), failure isolation and retry, timeout isolation,
delete cleanup, restart recovery, atomic publication under concurrent
reads, and incremental-vs-full artifact equivalence.
