# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from fogmap import __version__, basemap, composite, db, raster
from fogmap.ingest import common, gpx, tcx

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "X-FogMap-Token"

TILE_CACHE_CONTROL = "public, max-age=300, must-revalidate"
BASEMAP_NAME = re.compile(r"^[A-Za-z0-9._-]+\.pmtiles$")
RANGE_HEADER = re.compile(r"^bytes=(\d*)-(\d*)$")
BASEMAP_CHUNK = 1024 * 256


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One connection at startup purely to create the schema. Requests open
    # their own, because handlers that do real work run in a worker thread and
    # a SQLite connection belongs to the thread that made it.
    conn = db.open_initialised()
    conn.close()

    # Held in memory so a tile miss is answered without rendering anything in
    # the request path. Invariant 3 allows a file read and nothing else.
    app.state.placeholders = {
        (theme, kind): composite.placeholder_tile(theme, kind)
        for theme in composite.THEMES
        for kind in composite.KINDS
    }
    yield


app = FastAPI(
    title="FogMap",
    version=__version__,
    summary="Self-hosted fog-of-war location map",
    lifespan=lifespan,
)


def get_conn() -> Iterator[sqlite3.Connection]:
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@app.middleware("http")
async def require_token_on_mutations(request: Request, call_next):
    """Shared-token gate on every mutating route.

    This is a doorstop, not a security model. It exists so that a misbehaving
    IoT device or a stray curl cannot wipe location history.
    """
    if request.method not in MUTATING_METHODS:
        return await call_next(request)

    expected = os.environ.get("FOGMAP_TOKEN", "").strip()
    if not expected:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "FOGMAP_TOKEN is not set on the server, so every mutating "
                    "request is refused. Set FOGMAP_TOKEN in the api "
                    "environment and restart the container."
                )
            },
        )

    presented = request.headers.get(TOKEN_HEADER, "")
    if not presented:
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    f"Missing {TOKEN_HEADER} header. Every POST, PATCH and "
                    "DELETE request must present the shared token."
                )
            },
        )

    if not secrets.compare_digest(presented, expected):
        return JSONResponse(
            status_code=401,
            content={
                "detail": (
                    f"The {TOKEN_HEADER} header does not match the token "
                    "configured on this server."
                )
            },
        )

    return await call_next(request)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """Liveness plus the one fact worth checking first, the version."""
    return {"status": "ok", "version": __version__}


@app.get("/api/meta")
def meta(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    """Version, available layers, data extent and cache inventory."""
    return {
        "version": __version__,
        "layers": db.layer_inventory(conn),
        "views": composite.available_views(conn),
        "bbox": None,
        "counts": db.counts(conn),
        "blobs_by_kind": db.blob_counts_by_kind(conn),
        "tiles": 0,
        "settings": db.get_settings(conn),
    }


def _ingest_upload(
    conn: sqlite3.Connection, parser, upload: UploadFile, source: str
) -> dict[str, int]:
    """Shared body of the file ingest endpoints.

    Declared sync so FastAPI runs it in a worker thread - rasterising a long
    track is CPU work and would otherwise stall the event loop.
    """
    filename = upload.filename or "upload"
    payload = upload.file.read()
    if not payload:
        raise HTTPException(
            status_code=400,
            detail=f"{filename} is empty. Nothing was imported.",
        )

    try:
        tracks = parser.parse(payload, filename=filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db.transaction(conn):
        result = common.ingest_tracks(conn, source, tracks)

    if result.events_created:
        # Re-render now rather than at request time, so the tile endpoint
        # stays a file read. Only the views this import changed are touched.
        root = tiles_root()
        root.mkdir(parents=True, exist_ok=True)
        composite.write_placeholders(root)
        for view in result.affected_views():
            composite.render_view(conn, root, view)

    return result.as_dict()


@app.post("/api/ingest/gpx")
def ingest_gpx(
    file: UploadFile, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, int]:
    return _ingest_upload(conn, gpx, file, "workout")


@app.post("/api/ingest/tcx")
def ingest_tcx(
    file: UploadFile, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, int]:
    return _ingest_upload(conn, tcx, file, "workout")


def tiles_root() -> Path:
    return db.data_dir() / "tiles"


@app.get("/api/tiles/{theme}/{view}/{kind}/{z}/{x}/{y}.png")
def tile(
    request: Request, theme: str, view: str, kind: str, z: int, x: int, y: int
) -> Response:
    """Serve one pre-rendered tile.

    A file read and nothing else - no rasterising, no compositing, no database
    query. Every bit of that work happened at ingest. This is invariant 3, and
    it is the reason the map does not care how large the archive is.
    """
    if theme not in composite.THEMES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown theme {theme!r}. Tiles exist for "
            f"{' and '.join(composite.THEMES)}.",
        )
    if kind not in composite.KINDS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown tile kind {kind!r}. Tiles exist for "
            f"{' and '.join(composite.KINDS)}.",
        )

    path = composite.tile_path(tiles_root(), theme, view, kind, z, x, y)
    if path.is_file():
        return FileResponse(
            path,
            media_type="image/png",
            headers={"Cache-Control": TILE_CACHE_CONTROL},
        )

    # Not a miss in the usual sense. Ground nobody has visited is not missing
    # data, it is unexplored, and unexplored ground is solid fog.
    return Response(
        content=request.app.state.placeholders[(theme, kind)],
        media_type="image/png",
        headers={"Cache-Control": TILE_CACHE_CONTROL},
    )


@app.api_route("/api/basemap/{name}", methods=["GET", "HEAD"])
def serve_basemap(request: Request, name: str) -> Response:
    """Serve a PMTiles archive, honouring HTTP range requests.

    MapLibre reads PMTiles by asking for byte ranges rather than downloading
    the archive, which is the only reason a planet-sized basemap is usable at
    all. Without 206 support the client would pull the whole file.
    """
    if not BASEMAP_NAME.match(name):
        raise HTTPException(
            status_code=404,
            detail=f"{name!r} is not a PMTiles archive name.",
        )

    path = db.data_dir() / name
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"No basemap at {path}. Download a Protomaps PMTiles archive "
                f"and place it in the data directory as {name}. The map renders "
                "fog and trails without one, but there will be nothing "
                "underneath them."
            ),
        )

    size = path.stat().st_size
    common_headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "public, max-age=86400",
        "Content-Type": "application/octet-stream",
    }

    if request.method == "HEAD":
        return Response(
            headers={**common_headers, "Content-Length": str(size)},
            media_type="application/octet-stream",
        )

    requested = request.headers.get("range")
    if not requested:
        return FileResponse(
            path, media_type="application/octet-stream", headers=common_headers
        )

    matched = RANGE_HEADER.match(requested.strip())
    if not matched:
        raise HTTPException(
            status_code=416,
            detail=f"Cannot parse Range header {requested!r}. Only "
            "'bytes=start-end' is supported.",
        )

    first, last = matched.group(1), matched.group(2)
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    elif last:
        start = max(0, size - int(last))  # a suffix range, the last N bytes
        end = size - 1
    else:
        raise HTTPException(status_code=416, detail=f"Empty range {requested!r}.")

    end = min(end, size - 1)
    if start > end or start >= size:
        return Response(
            status_code=416,
            headers={**common_headers, "Content-Range": f"bytes */{size}"},
        )

    length = end - start + 1

    def stream():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(BASEMAP_CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(
        stream(),
        status_code=206,
        media_type="application/octet-stream",
        headers={
            **common_headers,
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(length),
        },
    )


@app.get("/api/setup")
def setup_status() -> dict[str, object]:
    """What first-run setup still needs. Readable without a token."""
    from datetime import date

    return {
        "version": __version__,
        "basemap": basemap.basemap_status(),
        "suggested_urls": basemap.suggested_planet_urls(
            date.today().strftime("%Y%m%d")
        ),
        "data_dir": str(db.data_dir()),
    }


@app.post("/api/setup/basemap")
def setup_basemap(payload: dict) -> dict[str, object]:
    """Begin downloading a basemap archive into the data directory."""
    url = str(payload.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail=f"{url!r} is not an http or https URL.",
        )

    filename = str(payload.get("filename", "planet.pmtiles")).strip()
    if not BASEMAP_NAME.match(filename):
        raise HTTPException(
            status_code=400,
            detail=f"{filename!r} is not a valid PMTiles filename.",
        )

    try:
        return basemap.downloader.start(url, filename)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/setup/basemap")
def cancel_basemap() -> dict[str, object]:
    """Stop the running download. The partial file is kept so it can resume."""
    return basemap.downloader.cancel()


@app.post("/api/admin/rebuild")
def admin_rebuild(
    payload: dict | None = None, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Replay the event log and re-render the tile pyramid."""
    scope = (payload or {}).get("scope", "all")

    with db.transaction(conn):
        replayed, touched = raster.rebuild(conn)

    if scope == "all":
        views = composite.available_views(conn)
    elif isinstance(scope, str) and scope.startswith("view:"):
        views = [scope[len("view:") :]]
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Scope {scope!r} is not understood. Use 'all' or "
            "'view:<name>'.",
        )

    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.write_placeholders(root)
    rendered = {view: composite.render_view(conn, root, view) for view in views}

    return {
        "events_replayed": replayed,
        "tiles_touched": len(touched),
        "tiles_rendered": rendered,
    }
