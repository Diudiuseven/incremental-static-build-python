"""HTTP API.

Editing endpoints accept source-change notifications; the builder applies
them asynchronously (or synchronously via ``?wait=1``). All read requests
are served exclusively from the release that ``current`` points at, and
the route -> file lookup comes from that release's manifest. Because the
pointer switches atomically, a reader never observes mixed versions, a
half-rendered page, or a missing file during a new build.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, Response

from isg.engine import Engine
from isg.model import ChangeEvent, ChangeKind
from isg.sources import route_output_relpath

_CONTENT_TYPE = {
    ".html": "text/html; charset=utf-8",
    ".json": "application/json",
}


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="incremental-static-generator")
    app.state.engine = engine

    def _event(path: str, deleted: bool) -> ChangeEvent:
        kind = ChangeKind.DELETE if deleted else ChangeKind.UPSERT
        return ChangeEvent(kind=kind, path=path)

    # -- editing ---------------------------------------------------------
    @app.post("/events")
    def post_event(path: str = Query(...),
                   deleted: bool = False,
                   wait: bool = False) -> dict:
        engine.submit(_event(path, deleted))
        if wait and not engine.wait_idle(timeout=engine.config.page_timeout * 2):
            raise HTTPException(504, "build did not settle in time")
        return {"queued": path, "deleted": deleted}

    @app.post("/events/batch")
    def post_batch(events: list[dict],
                   wait: bool = False) -> dict:
        parsed = [ChangeEvent(ChangeKind(e["kind"]), e["path"])
                  for e in events]
        engine.submit_many(parsed)
        if wait and not engine.wait_idle(timeout=engine.config.page_timeout * 4):
            raise HTTPException(504, "build did not settle in time")
        return {"queued": len(parsed)}

    @app.post("/rebuild")
    def rebuild(full: bool = False) -> dict:
        info = engine.full_rebuild() if full else engine.rebuild_now()
        return {"release": info.release_id, "pages": len(info.pages)}

    # -- status ----------------------------------------------------------
    @app.get("/status")
    def status() -> dict:
        info = engine.current()
        return {
            "release": info.release_id if info else None,
            "pages": len(info.pages) if info else 0,
            "queued": len(engine.queue),
            "routes": sorted(info.pages) if info else [],
        }

    # -- consistent reads -----------------------------------------------
    @app.get("/__site/manifest.json")
    def manifest() -> JSONResponse:
        info = engine.current()
        if info is None:
            raise HTTPException(404, "no published release")
        payload = {
            "release_id": info.release_id,
            "created_at": info.created_at,
            "plan_revision": info.plan_revision,
            "pages": {route: digest for route, (digest, _obj)
                      in info.pages.items()},
        }
        return JSONResponse(payload)

    @app.get("/{route:path}")
    def serve(route: str) -> Response:  # noqa: C901 - flat route resolver
        if not route.startswith("/"):
            route = "/" + route
        candidates = [route]
        if not route.endswith(".html"):
            candidates.append(
                route_output_relpath(route if route.endswith("/")
                                     else route + "/"))

        data = engine.read_current_file(route)
        if data is None:
            alt = candidates[-1]
            mapped = "/" + alt.removesuffix("index.html").rstrip("/")
            mapped = mapped or "/"
            data = engine.read_current_file(mapped)
        if data is None:
            raise HTTPException(404, f"no such page: {route}")
        suffix = Path(route_output_relpath(route)).suffix
        return Response(content=data,
                        media_type=_CONTENT_TYPE.get(suffix, "text/html"))

    return app
