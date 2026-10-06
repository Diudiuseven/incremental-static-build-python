"""Development server entry point.

Environment variables
---------------------
ISG_SOURCE_DIR   source tree root   (default: ./site)
ISG_OUTPUT_DIR   release output root (default: ./.build)
ISG_CONCURRENCY, ISG_PAGE_TIMEOUT, ISG_BATCH_SIZE,
ISG_FULL_REBUILD_THRESHOLD, ISG_KEEP_RELEASES  build configuration
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
        keep_releases=int(os.environ.get("ISG_KEEP_RELEASES", "0")),
    )
    engine = Engine(
        source_root=os.environ.get("ISG_SOURCE_DIR", "site"),
        output_root=os.environ.get("ISG_OUTPUT_DIR", ".build"),
        config=config,
    )
    engine.start()
    return engine


app = create_app(build_engine())


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1",
                port=int(os.environ.get("ISG_PORT", "8000")))
