"""FastAPI application exposing the engine over HTTP.

Endpoints
---------
* ``GET  /...``                 serves files from the *currently published*
                                release via a symlink-resolved StaticFiles
                                mount (never the staging directory)
* ``POST /__admin/edits``       submit upserts/deletes, wait for build+publish
* ``POST /__admin/rebuild``     force a full rebuild
* ``GET  /__admin/status``      current release id and last build outcome
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .fsutil import UnsafePathError, normalize_rel
from .model import Edit, Op


class EditDTO(BaseModel):
    path: str = Field(min_length=1)
    op: str = "upsert"
    # UTF-8 text content for upserts; raw bytes are transported as text.
    content: str | None = None


class EditsRequest(BaseModel):
    edits: list[EditDTO]


def create_app(engine) -> FastAPI:
    app = FastAPI(title="incremental-static-build", version="0.1.0")
    app.state.engine = engine

    def _outcome_dict(outcome) -> dict[str, Any]:
        return {
            "success": outcome.success,
            "ok": sorted(outcome.ok),
            "failed": outcome.failed,
            "timed_out": outcome.timed_out,
        }

    @app.post("/__admin/edits")
    def submit_edits(req: EditsRequest) -> JSONResponse:
        try:
            edits = [
                Edit(
                    path=normalize_rel(dto.path),
                    op=Op(dto.op),
                    content=None if dto.content is None else dto.content.encode("utf-8"),
                )
                for dto in req.edits
            ]
        except (ValueError, UnsafePathError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        outcome = engine.submit_edits(edits)
        status = 200 if outcome.success else 409
        return JSONResponse(
            status_code=status,
            content={
                "build": _outcome_dict(outcome),
                "release_id": engine.last_release_id,
                "plan_reason": engine.last_plan_reason,
            },
        )

    @app.post("/__admin/rebuild")
    def rebuild() -> JSONResponse:
        outcome = engine.rebuild_full()
        status = 200 if outcome.success else 409
        return JSONResponse(status_code=status,
                            content={"build": _outcome_dict(outcome),
                                     "release_id": engine.last_release_id})

    @app.get("/__admin/status")
    def status() -> dict[str, Any]:
        last = engine.last_build
        return {
            "current_release_id": engine.releases.current_release_id(),
            "last_build": _outcome_dict(last) if last is not None else None,
            "last_plan_reason": engine.last_plan_reason,
        }

    @app.get("/{path:path}")
    def serve(path: str) -> FileResponse:
        site = engine.site_path()
        if site is None:
            raise HTTPException(status_code=503, detail="site not published yet")
        try:
            rel = normalize_rel(path)
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        candidate = os.path.join(site, rel)
        if rel and os.path.isfile(candidate):
            return FileResponse(candidate)
        index = os.path.join(site, rel, "index.html") if rel \
            else os.path.join(site, "index.html")
        if os.path.isfile(index):
            return FileResponse(index)
        raise HTTPException(status_code=404, detail="not found")

    return app
