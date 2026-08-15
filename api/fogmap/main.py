# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from fogmap import __version__, basemap, composite, db, places, raster, tokens
from fogmap.ingest import common, gpx, live, tcx

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "X-FogMap-Token"

TILE_CACHE_CONTROL = "public, max-age=300, must-revalidate"
BASEMAP_NAME = re.compile(r"^[A-Za-z0-9._-]+\.pmtiles$")
RANGE_HEADER = re.compile(r"^bytes=(\d*)-(\d*)$")
BASEMAP_CHUNK = 1024 * 256

# These check the token themselves rather than being gated on the HTTP verb,
# because whether they need one, and which header carries it, depends on the
# request. The live endpoints also have to answer 503 when their source is
# switched off before authentication is considered at all.
SELF_GUARDED_PATHS = frozenset(
    {
        "/api/setup/basemap",
        "/api/ingest/overland",
        "/api/ingest/owntracks",
        "/api/ingest/ha",
    }
)

# Fetching a published basemap from a known public source changes nobody's
# history - it downloads public map data into a cache. Asking someone to go
# and find a token in a .env file before the app will fetch its own basemap
# makes the first five minutes worse for no gain. A URL of the user's own is
# different: that makes this server fetch an address someone else supplied,
# which is worth gating.
TRUSTED_BASEMAP_HOSTS = frozenset({"build.protomaps.com"})


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

    conn = db.open_initialised()
    try:
        app.state.token, app.state.token_source = tokens.resolve(conn)
    finally:
        conn.close()

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


def effective_token(request: Request) -> tuple[str, str]:
    """The token in force, and where it came from.

    The environment is read on every call rather than once at startup, so
    changing FOGMAP_TOKEN takes effect without anyone having to reason about
    when it was last looked at. The generated fallback is resolved once, at
    startup, because it lives in the database.
    """
    from_env = os.environ.get(tokens.ENV_NAME, "").strip()
    if from_env:
        return from_env, "environment"
    return str(getattr(request.app.state, "token", "") or ""), "generated"


def expected_token(request: Request) -> str:
    return effective_token(request)[0]


def token_error(request: Request) -> tuple[int, str] | None:
    """Check the shared token. Returns (status, detail) when it is not right."""
    expected = expected_token(request)
    if not expected:
        return (
            503,
            "This server has no API token, which should not be possible - one "
            "is generated on first start. Restart the api container.",
        )

    presented = request.headers.get(TOKEN_HEADER, "")
    if not presented:
        return (
            401,
            f"Missing {TOKEN_HEADER} header. Every POST, PATCH and DELETE "
            "request must present the shared token.",
        )

    if not secrets.compare_digest(presented, expected):
        return (
            401,
            f"The {TOKEN_HEADER} header does not match the token configured "
            "on this server.",
        )
    return None


@app.middleware("http")
async def require_token_on_mutations(request: Request, call_next):
    """Shared-token gate on every mutating route.

    This is a doorstop, not a security model. It exists so that a misbehaving
    IoT device or a stray curl cannot wipe location history.
    """
    if request.method not in MUTATING_METHODS:
        return await call_next(request)
    if request.url.path in SELF_GUARDED_PATHS:
        return await call_next(request)

    failure = token_error(request)
    if failure is not None:
        return JSONResponse(status_code=failure[0], content={"detail": failure[1]})

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
        _render_views(conn, result.affected_views(), result.tiles_touched)

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


def _live_token_ok(request: Request) -> bool:
    """Overland sends a bearer token; everything else sends the header.

    Overland has no way to add an arbitrary header, so its own mechanism is
    accepted as well rather than making the app unusable with it.
    """
    expected = expected_token(request)
    if not expected:
        return False

    presented = request.headers.get(TOKEN_HEADER, "")
    if presented and secrets.compare_digest(presented, expected):
        return True

    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return secrets.compare_digest(authorization[7:].strip(), expected)
    return False


def _ingest_live(
    request: Request, conn: sqlite3.Connection, source: str, payload: object
) -> dict[str, object]:
    """Shared body of the three live endpoints."""
    if not live.is_enabled(conn, source):
        raise HTTPException(
            status_code=503,
            detail=(
                f"The {source} ingest endpoint is switched off. Enable it under "
                "Settings, data sources, in the FogMap web interface. Nothing "
                "was recorded."
            ),
        )

    if not _live_token_ok(request):
        raise HTTPException(
            status_code=401,
            detail=(
                f"Live ingest needs the shared token, as {TOKEN_HEADER} or as "
                "an Authorization bearer token."
            ),
        )

    try:
        # Lower-cased because OwnTracks identifies the user and device in
        # X-Limit-U and X-Limit-D headers rather than in the body.
        headers = {name.lower(): value for name, value in request.headers.items()}
        fixes, meta = live.PARSERS[source](payload, headers)
    except live.LiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db.transaction(conn):
        result = live.append(conn, source, fixes, meta)

    if result.accepted:
        _render_views(
            conn,
            _views_for_layers(_live_layers(conn, result.event_id)),
            result.tiles_touched,
        )

    return result.as_dict()


def _live_layers(conn: sqlite3.Connection, event_id: int | None) -> list[str]:
    if event_id is None:
        return ["all"]
    row = conn.execute(
        "SELECT layers FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    return json.loads(row["layers"]) if row else []


@app.post("/api/ingest/overland")
async def ingest_overland(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    payload = await _json_body(request)
    return await run_in_threadpool(_ingest_live, request, conn, "overland", payload)


@app.post("/api/ingest/owntracks")
async def ingest_owntracks(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    # A zero-length body, which OwnTracks posts when a friend is deleted,
    # parses to no fixes and is accepted quietly. It is handled down in the
    # worker thread with everything else: the database connection belongs to
    # that thread, so touching it from the event loop here would fail.
    payload = await _json_body(request)
    return await run_in_threadpool(_ingest_live, request, conn, "owntracks", payload)


@app.post("/api/ingest/ha")
async def ingest_ha(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    payload = await _json_body(request)
    return await run_in_threadpool(_ingest_live, request, conn, "ha", payload)


async def _json_body(request: Request) -> object:
    raw = await request.body()
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail=f"Body is not valid JSON ({exc})."
        ) from exc


@app.get("/api/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    return {
        "settings": db.get_settings(conn),
        "sources": [
            {
                "source": source,
                "enabled": live.is_enabled(conn, source),
                "has_events": live.has_events(conn, source),
            }
            for source in live.LIVE_SOURCES
        ],
    }


@app.patch("/api/settings")
def patch_settings(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400, detail="Send an object of settings to change."
        )

    with db.transaction(conn):
        for key, value in payload.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

    return get_settings(conn)


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


def _render_views(
    conn: sqlite3.Connection,
    views: list[str],
    touched: set[tuple[int, int]] | None = None,
) -> None:
    """Re-render views, limited to the ground an edit actually covered.

    `touched` is the set of z14 tiles an event wrote. Passing it turns a
    several-second whole-view re-encode into fifteen tiles, which is the
    difference between undo feeling instant and looking like it did nothing.
    """
    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.write_placeholders(root)
    scope = None if touched is None else composite.rebuild_scope(touched)
    for view in views:
        composite.render_view(conn, root, view, scope=scope)


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
        composite.views_touching(conn, touched)
        if op == "erase"
        else _views_for_layers(layers),
        touched,
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
    was_erase = row["op"] == "erase"
    before = set(composite.available_views(conn))

    with db.transaction(conn):
        tiles = raster.event_tiles(row)
        conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        raster.rebuild_tiles(conn, tiles)

    # An erase was subtracted from every view, so removing it puts fog back
    # into every view. Anything else only ever touched its own layers.
    _render_views(
        conn,
        composite.views_touching(conn, tiles)
        if was_erase
        else _views_for_layers(layers),
        tiles,
    )

    # Deleting the last event of a year retires that year as a view. Its
    # directory goes with it, or the tile endpoint keeps serving a year that
    # no longer exists.
    _retire_views(before - set(composite.available_views(conn)))

    return {"deleted": event_id, "tiles_rebuilt": len(tiles)}


def _retire_views(gone: set[str]) -> None:
    root = tiles_root()
    for view in gone:
        for theme in composite.THEMES:
            shutil.rmtree(root / theme / view.replace(":", "-"), ignore_errors=True)


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


TRAIL_FEATURE_CAP = 500

# The trail endpoint is for a zoomed-in viewport. Section 1 is explicit that
# no response may scale with point count except this one, and only because it
# is bounded by the viewport - so a request for half the planet is refused
# rather than quietly answered with whatever fits under the cap.
TRAIL_MAX_SPAN_DEG = 2.0


@app.get("/api/trails")
def trails(
    bbox: str,
    layer: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """Individual tracks in a viewport, as GeoJSON, for click-to-identify.

    Above z14 the raster has less detail than the geometry behind it, so the
    lines themselves are worth sending. Hard capped, and only over an area
    small enough to be a real viewport.
    """
    parts = bbox.split(",")
    if len(parts) != 4:
        raise HTTPException(
            status_code=400,
            detail=f"bbox must be 'west,south,east,north', got {bbox!r}.",
        )
    try:
        west, south, east, north = (float(part) for part in parts)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"bbox values must be numbers, got {bbox!r}.",
        ) from None

    if west > east or south > north:
        raise HTTPException(
            status_code=400,
            detail=(
                f"bbox {bbox!r} is inside out. Expected west,south,east,north "
                "with west below east and south below north."
            ),
        )

    if (east - west) > TRAIL_MAX_SPAN_DEG or (north - south) > TRAIL_MAX_SPAN_DEG:
        raise HTTPException(
            status_code=400,
            detail=(
                f"bbox spans {east - west:.2f} by {north - south:.2f} degrees. "
                f"Trails are served for viewports up to {TRAIL_MAX_SPAN_DEG} "
                "degrees across, which is why zooming in is required - the "
                "raster tiles cover everything wider."
            ),
        )

    features: list[dict[str, object]] = []
    truncated = False

    for row in conn.execute(
        "SELECT * FROM events WHERE op = 'add' ORDER BY id DESC"
    ):
        if layer and layer not in raster.parse_layers(row["layers"], int(row["id"])):
            continue

        try:
            points = raster.geometry_points(row["geometry"], int(row["id"]))
        except ValueError:
            continue
        if not points:
            continue

        lons = [lon for lon, _ in points]
        lats = [lat for _, lat in points]
        if max(lons) < west or min(lons) > east:
            continue
        if max(lats) < south or min(lats) > north:
            continue

        if len(features) >= TRAIL_FEATURE_CAP:
            truncated = True
            break

        features.append(
            {
                "type": "Feature",
                "id": int(row["id"]),
                "geometry": json.loads(row["geometry"]),
                "properties": {
                    "id": int(row["id"]),
                    "source": row["source"],
                    "layers": json.loads(row["layers"]),
                    "radius_m": row["radius_m"],
                    "created_at": row["created_at"],
                    "meta": json.loads(row["meta"]) if row["meta"] else None,
                },
            }
        )

    return {
        "type": "FeatureCollection",
        "features": features,
        "truncated": truncated,
        "cap": TRAIL_FEATURE_CAP,
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
def setup_status(request: Request) -> dict[str, object]:
    """What first-run setup still needs. Readable without a token."""
    from datetime import date

    return {
        "version": __version__,
        "token": dict(zip(("value", "source"), effective_token(request))),
        "basemap": basemap.basemap_status(),
        "suggested_urls": basemap.suggested_planet_urls(
            date.today().strftime("%Y%m%d")
        ),
        "data_dir": str(db.data_dir()),
    }


def is_trusted_basemap(url: str) -> bool:
    """Is this one of the published builds FogMap offers by name?"""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in TRUSTED_BASEMAP_HOSTS


@app.post("/api/setup/basemap")
def setup_basemap(request: Request, payload: dict) -> dict[str, object]:
    """Begin downloading a basemap archive into the data directory.

    One of the offered public builds needs no token: it fetches public map
    data on first run, before the user has had a chance to configure anything.
    A URL of their own does need one, because that points this server at an
    address it was given.
    """
    url = str(payload.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail=f"{url!r} is not an http or https URL.",
        )

    if not is_trusted_basemap(url):
        failure = token_error(request)
        if failure is not None:
            raise HTTPException(
                status_code=failure[0],
                detail=(
                    f"{failure[1]} A basemap URL of your own points this "
                    "server at an address you supplied, so it needs the token. "
                    "The offered Protomaps builds do not."
                ),
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
def cancel_basemap(discard: bool = False) -> dict[str, object]:
    """Stop the running download.

    By default this is a pause: the partial file stays, so starting again
    resumes from where it stopped. `discard=true` throws those bytes away,
    which after several hours of downloading is worth being deliberate about.
    """
    return basemap.downloader.cancel(discard=discard)


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
