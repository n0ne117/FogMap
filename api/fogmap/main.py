# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from fogmap import __version__, basemap, composite, db, places, raster
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

    # A basemap download runs for hours, so a restart during one is the normal
    # way it ends rather than an edge case. Pick it up again.
    if basemap.downloader.resume_if_interrupted():
        print("resuming interrupted basemap download", flush=True)

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


def _render_views(conn: sqlite3.Connection, views: list[str]) -> None:
    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.write_placeholders(root)
    for view in views:
        composite.render_view(conn, root, view)


def _views_for_layers(layers: list[str]) -> list[str]:
    views = ["all"]
    views += sorted(f"year:{layer}" for layer in layers if layer.isdigit())
    if common.PREHISTORY in layers:
        views.append(common.PREHISTORY)
    return views


@app.post("/api/events", status_code=201)
def create_event(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Record one drawn stroke.

    A brush stroke is not a special case. It becomes a LineString event and
    goes down exactly the path a GPX import takes, which is why an erase drawn
    by hand survives a rebuild the same way everything else does.
    """
    source = str(payload.get("source", "manual"))
    if source not in common.RADIUS_DEFAULTS_M:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source {source!r}. Valid sources are "
            f"{', '.join(sorted(common.RADIUS_DEFAULTS_M))}.",
        )

    op = str(payload.get("op", "add"))
    if op not in ("add", "erase"):
        raise HTTPException(
            status_code=400,
            detail=f"Unknown op {op!r}. FogMap stores only 'add' and 'erase'.",
        )

    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(
            status_code=400,
            detail="geometry must be a GeoJSON Point or LineString object.",
        )

    # Not `or`: a radius of 0 is falsy, and would silently become the default
    # instead of being refused.
    given = payload.get("radius_m")
    try:
        radius_m = (
            common.RADIUS_DEFAULTS_M[source] if given is None else float(given)
        )
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail=f"radius_m must be a number, got {given!r}."
        ) from None

    if radius_m <= 0:
        raise HTTPException(
            status_code=400,
            detail=f"radius_m must be greater than 0 m, got {radius_m}.",
        )

    try:
        layers = (
            [raster.ERASE_LAYER]
            if op == "erase"
            else common.expand_layers(payload.get("layers"))
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    encoded = json.dumps(geometry)
    with db.transaction(conn):
        cursor = conn.execute(
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                source,
                op,
                encoded,
                radius_m,
                json.dumps(layers),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(payload.get("meta")) if payload.get("meta") else None,
            ),
        )
        event_id = int(cursor.lastrowid)
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

        try:
            touched = raster.stamp_event(conn, row)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # An erase is subtracted from every view when one is composed, not just
    # from the layer it was drawn in, so every view has to be re-rendered.
    # Rendering only the erase event's own layers leaves every year view
    # showing fog that has just been rubbed out.
    _render_views(
        conn,
        composite.available_views(conn)
        if op == "erase"
        else _views_for_layers(layers),
    )

    return {
        "id": event_id,
        "op": op,
        "layers": layers,
        "radius_m": radius_m,
        "tiles_touched": len(touched),
    }


@app.delete("/api/events/{event_id}")
def delete_event(
    event_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Remove an event and rebuild only the ground it covered.

    Fog and trail accumulate, so an event cannot be subtracted - the tiles it
    touched are rebuilt from whatever events remain. This is undo.
    """
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No event with id {event_id}.")

    layers = raster.parse_layers(row["layers"], event_id)

    with db.transaction(conn):
        tiles = raster.event_tiles(row)
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        raster.rebuild_tiles(conn, tiles)

    # Deleting can empty a view completely, and an erase applied to every
    # view anyway. render_all prunes tiles and views that no longer have
    # anything behind them, which rendering a named list would not.
    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.render_all(conn, root)

    return {"deleted": event_id, "tiles_rebuilt": len(tiles)}


@app.get("/api/events")
def list_events(
    source: str | None = None,
    layer: str | None = None,
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)

    where: list[str] = []
    params: list[object] = []
    if source:
        where.append("source = ?")
        params.append(source)
    if layer:
        where.append("layers LIKE ?")
        params.append(f'%"{layer}"%')

    clause = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM events{clause}", params
    ).fetchone()["n"]

    rows = conn.execute(
        f"SELECT id, source, op, radius_m, layers, external_id, created_at, meta "
        f"FROM events{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return {
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "events": [
            {
                "id": row["id"],
                "source": row["source"],
                "op": row["op"],
                "radius_m": row["radius_m"],
                "layers": json.loads(row["layers"]),
                "external_id": row["external_id"],
                "created_at": row["created_at"],
                "meta": json.loads(row["meta"]) if row["meta"] else None,
            }
            for row in rows
        ],
    }


@app.get("/api/places")
def get_places(
    person: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    return {
        "places": places.listing(conn, person),
        "people": places.people(conn),
        "categories": list(places.CATEGORIES),
    }


@app.post("/api/places", status_code=201)
def post_place(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Add a place. Creating one clears the fog around it."""
    try:
        with db.transaction(conn):
            place, layers = places.create(conn, payload)
    except places.PlaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _render_views(conn, _views_for_layers(layers))
    return place


@app.patch("/api/places/{place_id}")
def patch_place(
    place_id: int, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            place, layers, dirty = places.update(conn, place_id, payload)
            if dirty:
                raster.rebuild_tiles(conn, dirty)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No place with id {place_id}."
        ) from exc
    except places.PlaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Moving a place or changing its dates can empty the tiles it used to
    # cover, so render everything and let the pruning sort it out.
    if dirty:
        composite.render_all(conn, tiles_root())
    else:
        _render_views(conn, _views_for_layers(layers))
    return place


@app.delete("/api/places/{place_id}")
def remove_place(
    place_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            place, tiles = places.delete(conn, place_id)
            raster.rebuild_tiles(conn, tiles)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No place with id {place_id}."
        ) from exc

    composite.render_all(conn, tiles_root())
    return {"deleted": place["id"], "name": place["name"]}


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
