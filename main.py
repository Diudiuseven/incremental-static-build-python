"""Development server entry point.

Configuration via environment variables
---------------------------------------
ISG_SOURCE_DIR                source tree root   (default: ./site)
ISG_OUTPUT_DIR                release output root (default: ./.build)
ISG_CONCURRENCY               parallel page renders
ISG_PAGE_TIMEOUT              per-page build timeout (seconds)
ISG_BATCH_SIZE                max change events per incremental batch
ISG_FULL_REBUILD_THRESHOLD    affected-page fraction triggering full rebuild
ISG_KEEP_RELEASES             historical release trees retained
ISG_PORT                      HTTP port for the dev server
"""

from __future__ import annotations

import os

import uvicorn

from isg.api import create_app
from isg.config import Config
from isg.engine import Engine


def build_engine() -> Engine:
    config = Config(
        concurrency=int(os.environ.get("ISG_CONCURRENCY", "4")),
        page_timeout=float(os.environ.get("ISG_PAGE_TIMEOUT", "10")),
        batch_size=int(os.environ.get("ISG_BATCH_SIZE", "100")),
        full_rebuild_threshold=float(
            os.environ.get("ISG_FULL_REBUILD_THRESHOLD", "0.5")),
        keep_releases=int(os.environ.get("ISG_KEEP_RELEASES", "1")),
        batch_idle_seconds=float(os.environ.get("ISG_BATCH_IDLE", "0.05")),
    )
    engine = Engine(
        source_root=os.environ.get("ISG_SOURCE_DIR", "site"),
        output_root=os.environ.get("ISG_OUTPUT_DIR", ".build"),
        config=config,
    )
    # Publish an initial full release so reads are immediately consistent.
    engine.rebuild_now()
    engine.start()
    return engine


engine = build_engine()
app = create_app(engine)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("ISG_PORT", "8000")))
