# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from irfaran import (  # noqa: I001
    __version__,
    basemap,
    composite,
    db,
    organise,
    places,
    raster,
    history,
    settings_env,
    tokens,
    trackers,
    transfer,
)
from irfaran.ingest import common, gpx, live, tcx

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "X-Irfaran-Token"

# The header this was called before the project was renamed. A tracker app
# configured months ago is not going to reconfigure itself, and an install
# that starts rejecting its own tracker on upgrade is worse than a spelling
# nobody sees.
LEGACY_TOKEN_HEADER = "X-FogMap-Token"

TILE_CACHE_CONTROL = "public, max-age=300, must-revalidate"


def tile_validators(path: Path) -> tuple[str, str]:
    """An ETag and Last-Modified for a rendered tile.

    Derived from the file's modification time and size, which is what changes
    when a tile is re-rendered - and cheap, because the stat has to happen
    anyway to know the file is there.
    """
    stat = path.stat()
    tag = hashlib.md5(
        f"{stat.st_mtime_ns}-{stat.st_size}".encode(), usedforsecurity=False
    ).hexdigest()
    return f'"{tag}"', formatdate(stat.st_mtime, usegmt=True)


def unchanged(request: Request, etag: str, last_modified: str = "") -> bool:
    """Has the client already got this exact tile?

    Answering that with a 304 is the difference between a pan over old ground
    costing a few hundred kilobytes and costing nothing. The validators were
    always sent; nothing ever checked them coming back, so every revalidation
    re-sent the whole PNG.

    If-None-Match wins outright when present, per RFC 9110: it is exact, where
    a date comparison has a one-second resolution and a re-render inside the
    same second would go unnoticed.
    """
    if_none_match = request.headers.get("if-none-match")
    if if_none_match:
        candidates = {tag.strip() for tag in if_none_match.split(",")}
        return etag in candidates or "*" in candidates

    since = request.headers.get("if-modified-since")
    if since:
        try:
            return parsedate_to_datetime(since) >= parsedate_to_datetime(last_modified)
        except (TypeError, ValueError):
            return False
    return False


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


def load_placeholders(app: FastAPI, conn: sqlite3.Connection) -> None:
    """Cache the empty tiles in memory.

    Held in memory so a tile miss is answered without rendering anything in
    the request path. Invariant 3 allows a file read and nothing else. Rebuilt
    whenever the fog colour changes, or most of the world would stay the old
    colour - a tile with no data is the common case, not the exception.
    """
    app.state.placeholders = {
        (theme, kind): composite.placeholder_tile(theme, kind, conn)
        for theme in composite.THEMES
        for kind in composite.KINDS
    }

    # An ETag per placeholder, taken from the bytes themselves.
    #
    # These are the majority of what a browser asks for - a fog-of-war map is
    # mostly unexplored, so most of any screenful has no file behind it - and
    # they used to go out with no validator at all, which meant every one was
    # re-sent in full on every revalidation, forever. Hashing the content means
    # the tag changes by itself when the fog colour does, because that is
    # exactly when this dictionary is rebuilt.
    app.state.placeholder_etags = {
        key: '"%s"' % hashlib.md5(body, usedforsecurity=False).hexdigest()
        for key, body in app.state.placeholders.items()
    }


# How often to wake up and ask whether any tracker is due. Not how often a
# tracker syncs - that is its own setting, in hours. This only has to be finer
# grained than the shortest interval anyone would sensibly choose.
TRACKER_TICK_S = 600


def sync_due_trackers() -> list[str]:
    """Sync every tracker whose timer has come round, and draw what arrived.

    Its own connection, because this runs in a worker thread and a SQLite
    connection belongs to the thread that opened it.

    Errors are written to the tracker's own last_error and go no further. A
    service being down, or a key having been revoked, is not a reason for the
    next tick to stop happening.
    """
    done: list[str] = []
    conn = db.connect()
    try:
        for name in trackers.TRACKERS:
            if not trackers.is_due(conn, name):
                continue
            try:
                result = trackers.sync(conn, name)
                done.append(f"{name}: {result.summary()}")
                if result.tiles:
                    drain_pending_render(conn)
            except trackers.TrackerError as exc:
                with db.transaction(conn):
                    trackers.put(conn, name, "last_error", str(exc))
                    # Stamped even on failure, or a server that cannot reach
                    # the service would retry on every single tick.
                    trackers.put(
                        conn,
                        name,
                        "last_sync",
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )
                done.append(f"{name}: {exc}")
    finally:
        conn.close()
    return done


async def tracker_ticker() -> None:
    """Check the trackers on a timer, for as long as the app is up."""
    while True:
        try:
            await asyncio.sleep(TRACKER_TICK_S)
            for line in await run_in_threadpool(sync_due_trackers):
                print(f"tracker sync - {line}", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the loop outlives any one failure
            print(f"tracker tick failed: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One connection at startup purely to create the schema. Requests open
    # their own, because handlers that do real work run in a worker thread and
    # a SQLite connection belongs to the thread that made it.
    conn = db.open_initialised()
    try:
        load_placeholders(app, conn)
        app.state.token, app.state.token_source = tokens.resolve(conn)
    finally:
        conn.close()

    # A basemap download runs for hours, so a restart during one is the normal
    # way it ends rather than an edge case. Pick it up again.
    if basemap.downloader.resume_if_interrupted():
        print("resuming interrupted basemap download", flush=True)

    ticker = asyncio.create_task(tracker_ticker())
    try:
        yield
    finally:
        ticker.cancel()
        with suppress(asyncio.CancelledError):
            await ticker


app = FastAPI(
    title="Irfaran",
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
    changing IRFARAN_TOKEN takes effect without anyone having to reason about
    when it was last looked at. The generated fallback is resolved once, at
    startup, because it lives in the database.
    """
    from_env = settings_env.get(tokens.ENV_VAR)
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

    presented = request.headers.get(TOKEN_HEADER, "") or request.headers.get(
        LEGACY_TOKEN_HEADER, ""
    )
    if not presented:
        return (
            401,
            f"Missing {TOKEN_HEADER} header. Every POST, PATCH and DELETE "
            "request must present the shared token.",
        )

    if not secrets.compare_digest(presented, expected):
        # Naming the likely cause, because the wording of a mismatch sounds
        # like a malformed token when the common case is a perfectly good token
        # belonging to a different instance. Every server has its own.
        return (
            401,
            f"The {TOKEN_HEADER} header does not match this server's token. "
            "Each Irfaran instance has its own, so a token from another one is "
            "refused here - read this server's with "
            "`docker compose exec api python -m irfaran.cli token`.",
        )
    return None


@app.exception_handler(sqlite3.OperationalError)
async def busy_database(request: Request, exc: sqlite3.OperationalError):
    """A contended write is a "come back", not a "something broke".

    SQLite allows one writer at a time, and a tracker delivering hundreds of
    buffered fixes holds that writer for a while. Anything arriving behind it
    used to get a 500 - which tells a tracker its payload is bad, and a
    tracker that believes that may drop points nobody can recover. 503 with a
    Retry-After is the truth: the request was fine, the server was busy.
    """
    if "locked" not in str(exc) and "busy" not in str(exc):
        raise exc

    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "10"},
        content={
            "detail": (
                "The database was busy with another write and this one timed "
                f"out after {db.busy_timeout_s():.0f}s. Nothing was changed. "
                "Send it again."
            )
        },
    )


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
        "trail_ramps": sorted(composite.TRAIL_RAMP_SETS),
    }


def _ingest_upload(
    conn: sqlite3.Connection,
    parser,
    upload: UploadFile,
    source: str,
    defer_render: bool = False,
) -> dict[str, object]:
    """Shared body of the file ingest endpoints.

    Declared sync so FastAPI runs it in a worker thread - rasterising a long
    track is CPU work and would otherwise stall the event loop.

    `defer_render` is for bulk imports. Rendering costs roughly the whole
    archive rather than the file just added, so paying it once per file turns
    a few hundred workouts into an afternoon. Deferring records what is owed
    and leaves the tiles stale until something calls /api/render.
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
        history.record(
            conn, "error", "import", f"Could not read {filename}: {exc}",
            {"file": filename},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db.transaction(conn):
        result = common.ingest_tracks(conn, source, tracks)

    if result.events_created:
        if defer_render:
            with db.transaction(conn):
                db.defer_render(conn, result.tiles_touched)
        else:
            # Re-render now rather than at request time, so the tile endpoint
            # stays a file read. Only the views this import changed are touched.
            _render_views(conn, result.affected_views(), result.tiles_touched)

    if result.events_created:
        history.record(
            conn,
            "manual",
            "import",
            f"Imported {filename}: {result.events_created} "
            f"{'track' if result.events_created == 1 else 'tracks'}",
            {"file": filename, "points": result.points, "source": source},
        )
    else:
        history.record(
            conn,
            "manual",
            "import",
            f"Imported {filename}: nothing new, already here",
            {"file": filename, "skipped": result.events_skipped},
        )

    out = result.as_dict()
    out["render_pending"] = bool(defer_render and result.events_created)
    return out


@app.post("/api/ingest/gpx")
def ingest_gpx(
    file: UploadFile,
    defer_render: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    return _ingest_upload(conn, gpx, file, "workout", defer_render)


@app.post("/api/ingest/tcx")
def ingest_tcx(
    file: UploadFile,
    defer_render: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    return _ingest_upload(conn, tcx, file, "workout", defer_render)


def _live_token_ok(request: Request) -> bool:
    """Overland sends a bearer token; everything else sends the header.

    Overland has no way to add an arbitrary header, so its own mechanism is
    accepted as well rather than making the app unusable with it.
    """
    expected = expected_token(request)
    if not expected:
        return False

    presented = request.headers.get(TOKEN_HEADER, "") or request.headers.get(
        LEGACY_TOKEN_HEADER, ""
    )
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
                "Settings, data sources, in the Irfaran web interface. Nothing "
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

    if result.accepted or result.duplicates:
        history.record(
            conn,
            "source",
            f"live:{source}",
            f"{source} delivered {result.accepted} "
            f"{'fix' if result.accepted == 1 else 'fixes'}"
            + (f", {result.duplicates} already had" if result.duplicates else ""),
            {"source": source, "accepted": result.accepted},
            # A phone posting every few minutes would otherwise be the only
            # thing left in a capped history by tomorrow.
            coalesce=True,
        )

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
    """Overland's receiver endpoint.

    Overland decides whether a batch was received by looking for
    {"result": "ok"} in the body - not by the status code. Answering 200 with
    anything else means it keeps the batch queued and sends it again, forever:
    the points arrive and show up on the map, while the phone reports that the
    server never acknowledged them and quietly retries the same payload for
    the rest of the day. Extra keys are ignored, so the usual summary rides
    along beside it.
    """
    payload = await _json_body(request)
    result = await run_in_threadpool(
        _ingest_live, request, conn, "overland", payload
    )
    return {"result": "ok", **result}


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


FOG_COLOUR_KEYS = frozenset(
    composite.SETTING_FOG_COLOUR.format(theme=theme) for theme in composite.THEMES
)

# Settings baked into the tiles. Changing one of these costs a render; every
# other setting is either a server behaviour or a viewing choice the browser
# applies for free.
BAKED_KEYS = FOG_COLOUR_KEYS | {composite.SETTING_TRAIL_RAMP}


@app.patch("/api/settings")
def patch_settings(
    request: Request, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400, detail="Send an object of settings to change."
        )

    # Reject a bad value before it is stored, not when the render trips over it
    # and leaves the pyramid half written.
    recolour = BAKED_KEYS & {str(key) for key in payload}
    for key in recolour:
        try:
            if key == composite.SETTING_TRAIL_RAMP:
                composite.check_ramp(str(payload[key]))
            else:
                composite.parse_colour(str(payload[key]), f"Setting {key}")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    with db.transaction(conn):
        for key, value in payload.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

        # Names only. A settings value can be a token, and history is exported
        # with the archive.
        changed = sorted(str(key) for key in payload)
        if changed != ["ui_theme"]:
            history.record(
                conn,
                "system",
                "settings",
                f"Changed {', '.join(changed)}",
                {"keys": changed},
            )

    # These are baked into the tiles, so everything already on disk still has
    # the old ones. Every tile with data is marked as owing a render rather
    # than rendered here: on a real archive that is minutes of work, and doing
    # it inside the request means a request that times out somewhere between
    # the browser and the proxy with the tiles half rewritten.
    if recolour:
        # A trail ramp does not change a single fog pixel, and vice versa.
        kinds = ("trail",) if recolour == {composite.SETTING_TRAIL_RAMP} else ("fog",)
        with db.transaction(conn):
            db.defer_render(conn, composite.tiles_with_data(conn, None), kinds)
        load_placeholders(request.app, conn)

    out = get_settings(conn)
    return {**out, "render_pending": len(db.pending_render(conn))}


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
        etag, last_modified = tile_validators(path)
        headers = {
            "Cache-Control": TILE_CACHE_CONTROL,
            "ETag": etag,
            "Last-Modified": last_modified,
        }
        if unchanged(request, etag, last_modified):
            return Response(status_code=304, headers=headers)
        return FileResponse(path, media_type="image/png", headers=headers)

    # Not a miss in the usual sense. Ground nobody has visited is not missing
    # data, it is unexplored, and unexplored ground is solid fog.
    etag = request.app.state.placeholder_etags[(theme, kind)]
    headers = {"Cache-Control": TILE_CACHE_CONTROL, "ETag": etag}
    if unchanged(request, etag):
        return Response(status_code=304, headers=headers)
    return Response(
        content=request.app.state.placeholders[(theme, kind)],
        media_type="image/png",
        headers=headers,
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

    path = db.basemap_dir() / name
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


def _render_views_iter(
    conn: sqlite3.Connection,
    views: list[str],
    touched: set[tuple[int, int]] | None = None,
) -> Iterator[tuple[int, int]]:
    """Re-render views, yielding (jobs done, jobs total) as it goes.

    A stroke on a full archive is several seconds of rasterising into every
    view, and the only sign of it used to be the preview refusing to vanish.
    """
    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.write_placeholders(root, conn)
    scope = None if touched is None else composite.rebuild_scope(touched)
    yield from composite.render_views_iter(conn, root, views, scope=scope)


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
    composite.write_placeholders(root, conn)
    scope = None if touched is None else composite.rebuild_scope(touched)
    composite.render_views(conn, root, views, scope=scope)


@app.get("/api/export")
def export_archive(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> Response:
    """Everything worth keeping, as one file.

    Token-guarded by hand. The middleware only gates writes, and this is a GET
    - but it hands over the entire history of where somebody has been, which
    is the most sensitive thing here by a distance.
    """
    failure = token_error(request)
    if failure is not None:
        raise HTTPException(status_code=failure[0], detail=failure[1])

    name = transfer.export_name()
    return Response(
        content=transfer.export_bytes(conn),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/import")
async def import_archive(
    file: UploadFile, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Merge an export into this instance.

    Additive: nothing is deleted, nothing is overwritten, and importing the
    same file twice changes nothing the second time. The tiles it affects are
    marked as owing a render rather than rendered here, because on a real
    archive that is minutes of work and no HTTP request should be held open
    for it.
    """
    payload = await file.read()
    if not payload:
        raise HTTPException(
            status_code=400,
            detail=f"{file.filename or 'That file'} is empty. Nothing was imported.",
        )

    return await run_in_threadpool(_import_now, conn, payload)


def _import_now(conn: sqlite3.Connection, payload: bytes) -> dict[str, object]:
    """The import itself, off the event loop - it rasterises as it goes."""
    try:
        with db.transaction(conn):
            before = _highest_event_id(conn)
            result = transfer.import_archive(conn, payload)

            touched: set[tuple[int, int]] = set()
            for row in conn.execute(
                "SELECT * FROM events WHERE id > ? ORDER BY id", (before,)
            ):
                touched |= raster.stamp_event(conn, row)

            db.defer_render(conn, touched)
    except transfer.TransferError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"That export could not be read ({exc}).",
        ) from exc

    result["tiles_touched"] = len(touched)
    result["render_pending"] = len(db.pending_render(conn))
    return result


def _highest_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS top FROM events").fetchone()
    return int(row["top"])


@app.get("/api/render")
def render_status(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    pending = db.pending_render(conn)
    return {
        "pending_tiles": len(pending),
        "kinds": list(db.pending_kinds(conn)) if pending else [],
        "views": composite.views_touching(conn, pending) if pending else [],
        "workers": composite.render_workers(),
    }


@app.post("/api/render")
def render_pending() -> StreamingResponse:
    """Render everything a deferred import left owing, reporting as it goes.

    This is the other half of ingesting with defer_render: a bulk import
    stamps every file into the blob store and writes down which native tiles
    went stale, and this pays the render once for the lot instead of once per
    file.

    The body is newline-delimited JSON, one object per finished unit of work,
    because a render of a large archive takes long enough that a spinner is
    not an honest answer. The last line carries the summary.
    """

    def report() -> Iterator[str]:
        # Its own connection, deliberately. A dependency's connection is closed
        # when the handler returns, and a streaming handler returns before it
        # has done any of the work.
        conn = db.connect()
        try:
            pending = db.pending_render(conn)
            if not pending:
                yield json.dumps(
                    {"done": 0, "total": 0, "pending_tiles": 0, "views": [], "finished": True}
                ) + "\n"
                return

            views = composite.views_touching(conn, pending)
            kinds = db.pending_kinds(conn)
            root = tiles_root()
            root.mkdir(parents=True, exist_ok=True)
            composite.write_placeholders(root, conn)

            written: dict[str, int] = {}
            finished = jobs = 0
            for finished, jobs in composite.render_views_iter(
                conn,
                root,
                views,
                scope=composite.rebuild_scope(pending),
                written=written,
                kinds=kinds,
            ):
                yield json.dumps(
                    {"done": finished, "total": jobs, "kinds": list(kinds)}
                ) + "\n"

            with db.transaction(conn):
                db.clear_pending_render(conn)
                history.record(
                    conn,
                    "system",
                    "render",
                    f"Redrew {sum(written.values())} tiles across "
                    f"{len(views)} {'view' if len(views) == 1 else 'views'}",
                    {"tiles": sum(written.values()), "kinds": list(kinds)},
                )

            yield json.dumps(
                {
                    "done": finished,
                    "total": jobs,
                    "pending_tiles": len(pending),
                    "views": views,
                    "tiles_rendered": sum(written.values()),
                    "finished": True,
                }
            ) + "\n"
        finally:
            conn.close()

    return StreamingResponse(report(), media_type="application/x-ndjson")


def drain_pending_render(conn: sqlite3.Connection) -> int:
    """Render whatever is owing, with nobody watching.

    The streaming endpoint above is for a person holding a progress bar. A sync
    that ran on a timer has no such person, and leaving the tiles owing would
    mean an activity that arrived overnight not appearing until somebody
    happened to trigger a render.
    """
    pending = db.pending_render(conn)
    if not pending:
        return 0

    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.write_placeholders(root, conn)
    written = composite.render_views(
        conn,
        root,
        composite.views_touching(conn, pending),
        scope=composite.rebuild_scope(pending),
        kinds=db.pending_kinds(conn),
    )
    with db.transaction(conn):
        db.clear_pending_render(conn)
    return sum(written.values())


@app.get("/api/history")
def get_history(
    limit: int = 200,
    category: str | None = None,
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """What has happened, newest first.

    Readable without a token, like every other read here. It holds what was
    done rather than what is in the data - counts, filenames, setting names -
    and never a setting's value, because a value can be a token.
    """
    try:
        entries = history.recent(conn, limit, category)
    except history.HistoryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "entries": entries,
        "counts": history.counts(conn),
        "kept": {"entries": history.MAX_ENTRIES, "days": history.MAX_AGE_DAYS},
    }


@app.delete("/api/history")
def clear_history(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    with db.transaction(conn):
        gone = history.clear(conn)
    return {"cleared": gone}


# ------------------------------------------------------------ workout trackers


@app.get("/api/trackers")
def list_trackers(
    conn: sqlite3.Connection = Depends(get_conn),
) -> dict[str, object]:
    """What is configured, and never the keys themselves."""
    return {
        "trackers": [trackers.status(conn, name) for name in trackers.TRACKERS]
    }


@app.patch("/api/trackers/{name}")
def patch_tracker(
    name: str, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Change one tracker's configuration.

    An empty api_key is ignored rather than stored. The field comes back blank
    every time the page loads - the server will not say what the key is - so
    treating blank as "clear it" would wipe the key on any unrelated save.
    Clearing is what the switch is for.
    """
    try:
        trackers.check(name)
    except trackers.TrackerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if not isinstance(payload, dict) or not payload:
        raise HTTPException(
            status_code=400, detail="Send an object of tracker settings to change."
        )

    unknown = {str(key) for key in payload} - set(trackers.FIELDS)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot set {', '.join(sorted(unknown))} on a tracker. "
                f"Settable: {', '.join(trackers.FIELDS)}."
            ),
        )

    for field_name in ("sync_hours", "since_days"):
        if field_name in payload:
            try:
                int(str(payload[field_name]))
            except ValueError:
                raise HTTPException(
                    status_code=400,
                    detail=f"{field_name} has to be a whole number of "
                    + ("hours." if field_name == "sync_hours" else "days."),
                ) from None

    with db.transaction(conn):
        for field_name, value in payload.items():
            text = str(value).strip() if isinstance(value, str) else str(value)
            if field_name == "api_key" and not text:
                continue
            if field_name == "enabled":
                text = "true" if value in (True, "true", "True", 1, "1") else "false"
            trackers.put(conn, name, field_name, text)

    return trackers.status(conn, name)


@app.post("/api/trackers/{name}/sync")
def sync_tracker(name: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Fetch now, on purpose, reporting each activity as it lands.

    Runs even when the tracker's timer is switched off, because pressing the
    button is a clearer statement of intent than the timer setting is. It still
    needs a key, and being switched off entirely still means no.

    Newline-delimited JSON, for the same reason /api/render is: fifty activities
    is a minute or more of downloading, and a minute with no bytes sent is
    indistinguishable from a hang - to a person and to a reverse proxy.

    Whether it can start at all is settled before the stream begins, so a
    missing key is an ordinary 502 rather than an error smuggled inside a 200.
    """
    try:
        trackers.check(name)
    except trackers.TrackerError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        trackers.precheck(conn, name)
    except trackers.TrackerError as exc:
        with db.transaction(conn):
            trackers.put(conn, name, "last_error", str(exc))
            history.record(conn, "error", f"sync:{name}", str(exc))
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    def report() -> Iterator[str]:
        # Its own connection. A dependency's is closed when the handler
        # returns, and a streaming handler returns before doing any work.
        own = db.connect()
        try:
            for step in trackers.sync_iter(own, name):
                if step.get("finished"):
                    history.record(
                        own, "source", f"sync:{name}",
                        f"{name}: {step.get('summary')}",
                        {"imported": step.get("imported"), "no_gps": step.get("no_gps")},
                    )
                yield json.dumps(step) + "\n"
        except trackers.TrackerError as exc:
            with db.transaction(own):
                trackers.put(own, name, "last_error", str(exc))
                history.record(own, "error", f"sync:{name}", str(exc))
            yield json.dumps({"stage": "error", "error": str(exc)}) + "\n"
        finally:
            own.close()

    return StreamingResponse(report(), media_type="application/x-ndjson")


def _views_for_layers(layers: list[str]) -> list[str]:
    views = ["all"]
    views += sorted(f"year:{layer}" for layer in layers if layer.isdigit())
    if common.PREHISTORY in layers:
        views.append(common.PREHISTORY)
    return views


@app.post("/api/events", status_code=201)
def create_event(
    payload: dict,
    progress: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
):
    """Record one drawn stroke.

    With `?progress=1` the response is newline-delimited JSON - a line per
    finished unit of rendering, then a final line carrying what the plain
    response would have returned. The browser asks for that so a stroke on a
    large archive shows a bar rather than five silent seconds; everything else
    gets the single JSON object it always got.

    A brush stroke is not a special case. It becomes a LineString event and
    goes down exactly the path a GPX import takes, which is why an erase drawn
    by hand survives a rebuild the same way everything else does. An area is
    the same thing with a Polygon, and a reveal the same thing with an op that
    leaves the trail alone.
    """
    source = str(payload.get("source", "manual"))
    if source not in common.RADIUS_DEFAULTS_M:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown source {source!r}. Valid sources are "
            f"{', '.join(sorted(common.RADIUS_DEFAULTS_M))}.",
        )

    op = str(payload.get("op", "add"))
    if op not in raster.OPS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown op {op!r}. Irfaran stores only "
            f"{', '.join(repr(name) for name in raster.OPS)}.",
        )

    geometry = payload.get("geometry")
    if not isinstance(geometry, dict):
        raise HTTPException(
            status_code=400,
            detail="geometry must be a GeoJSON object: "
            f"{', '.join(raster.GEOMETRIES)}.",
        )
    if geometry.get("type") not in raster.GEOMETRIES:
        raise HTTPException(
            status_code=400,
            detail=f"Geometry type {geometry.get('type')!r} is not stored. "
            f"Irfaran stores {', '.join(raster.GEOMETRIES)}.",
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
    views = (
        composite.views_touching(conn, touched)
        if op == "erase"
        else _views_for_layers(layers)
    )

    result = {
        "id": event_id,
        "op": op,
        "layers": layers,
        "radius_m": radius_m,
        "tiles_touched": len(touched),
    }

    if source == "manual":
        history.record(
            conn,
            "manual",
            f"draw:{op}",
            {
                "add": "Drew a route",
                "reveal": "Cleared fog by hand",
                "erase": "Erased by hand",
            }.get(op, f"Drew {op}")
            + f" into {', '.join(layers)}",
            {"radius_m": radius_m, "tiles": len(touched), "geometry": geometry.get("type")},
        )

    if not progress:
        _render_views(conn, views, touched)
        return result

    def report() -> Iterator[str]:
        # Its own connection: a dependency's is closed when the handler
        # returns, and a streaming handler returns before doing the work. The
        # event is already committed by this point, so nothing is lost if the
        # client hangs up part way - the tiles are simply rendered without
        # anybody watching.
        own = db.connect()
        try:
            done = total = 0
            for done, total in _render_views_iter(own, views, touched):
                yield json.dumps({"done": done, "total": total}) + "\n"
            yield json.dumps({**result, "done": done, "total": total, "finished": True}) + "\n"
        finally:
            own.close()

    return StreamingResponse(
        report(), media_type="application/x-ndjson", status_code=201
    )


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
    was_op = str(row["op"]) if row["source"] == "manual" else ""
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

    history.record(
        conn,
        "manual",
        "undo",
        f"Removed a hand-drawn {was_op}" if was_op else "Removed an event",
        {"event": event_id, "tiles": len(tiles)},
    )
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

        if not _enters(points, west, south, east, north):
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


def _enters(
    points: list[tuple[float, float]],
    west: float,
    south: float,
    east: float,
    north: float,
) -> bool:
    """Does this track actually go through the viewport?

    Not "does its bounding box overlap" - that was the old test, and it is
    close to useless for the shape a track has. A ten kilometre run across a
    city has a bounding box covering the city, so every run in the archive
    passed the test for every viewport in it. The cap was hit every single
    time, the browser was handed five hundred tracks mostly nowhere near what
    was on screen, and the notice saying so never went away.

    The whole-track box is still the first thing checked, because rejecting on
    it is one comparison and it is right whenever it says no.
    """
    lons = [lon for lon, _ in points]
    lats = [lat for _, lat in points]
    if max(lons) < west or min(lons) > east:
        return False
    if max(lats) < south or min(lats) > north:
        return False

    if any(west <= lon <= east and south <= lat <= north for lon, lat in points):
        return True

    # A track can cross without putting a point inside - rare at GPS sampling
    # rates, but a hand-drawn line straight through is exactly that. Segment
    # boxes are tight enough to stand in for the segments themselves.
    for (a_lon, a_lat), (b_lon, b_lat) in zip(points, points[1:]):
        if max(a_lon, b_lon) < west or min(a_lon, b_lon) > east:
            continue
        if max(a_lat, b_lat) < south or min(a_lat, b_lat) > north:
            continue
        return True
    return False


@app.get("/api/places")
def get_places(
    person: str | None = None, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Every pin, plus the labels and folders needed to make sense of them.

    One request rather than three, because the sidebar cannot draw anything
    until it has all of it and three round trips is three chances to render
    half a tree.
    """
    return {
        "places": places.listing(conn, person),
        "labels": organise.labels(conn),
        "folders": organise.folders(conn),
        "people": places.people(conn),
        "categories": list(places.CATEGORIES),
    }


# ------------------------------------------------------------ labels, folders


def _organise_error(exc: organise.OrganiseError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@app.get("/api/labels")
def get_labels(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    return {"labels": organise.labels(conn)}


@app.post("/api/labels", status_code=201)
def post_label(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.create_label(conn, payload)
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.patch("/api/labels/{label_id}")
def patch_label(
    label_id: int, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.update_label(conn, label_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No label with id {label_id}."
        ) from exc
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.delete("/api/labels/{label_id}")
def remove_label(
    label_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.delete_label(conn, label_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No label with id {label_id}."
        ) from exc


@app.get("/api/people")
def get_people(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    """The registry, and every name actually used on a pin.

    Both, because they can differ: a name taken off the list stays on the pins
    that recorded it, and a pin restored from an older backup may name somebody
    who was never registered here.
    """
    return {"people": organise.people(conn), "named_on_pins": places.people(conn)}


@app.post("/api/people", status_code=201)
def post_person(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.create_person(conn, payload)
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.patch("/api/people/{person_id}")
def patch_person(
    person_id: int, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.update_person(conn, person_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Nobody on the list with id {person_id}."
        ) from exc
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.delete("/api/people/{person_id}")
def remove_person(
    person_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.delete_person(conn, person_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"Nobody on the list with id {person_id}."
        ) from exc


@app.get("/api/folders")
def get_folders(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    return {"folders": organise.folders(conn)}


@app.post("/api/folders", status_code=201)
def post_folder(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.create_folder(conn, payload)
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.patch("/api/folders/{folder_id}")
def patch_folder(
    folder_id: int, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    try:
        with db.transaction(conn):
            return organise.update_folder(conn, folder_id, payload)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No folder with id {folder_id}."
        ) from exc
    except organise.OrganiseError as exc:
        raise _organise_error(exc) from exc


@app.delete("/api/folders/{folder_id}")
def remove_folder(
    folder_id: int, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Delete a folder and its subfolders. The pins inside become unfiled."""
    try:
        with db.transaction(conn):
            return organise.delete_folder(conn, folder_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No folder with id {folder_id}."
        ) from exc


@app.post("/api/places", status_code=201)
def post_place(
    payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """Add a place. Creating one clears the fog around it."""
    try:
        with db.transaction(conn):
            place, layers, touched = places.create(conn, payload)
    except places.PlaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _render_views(conn, _views_for_layers(layers), touched)
    return place


@app.patch("/api/places/{place_id}")
def patch_place(
    place_id: int, payload: dict, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    before = set(composite.available_views(conn))
    try:
        with db.transaction(conn):
            place, layers, was, dirty = places.update(conn, place_id, payload)
            if dirty:
                raster.rebuild_tiles(conn, dirty)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail=f"No place with id {place_id}."
        ) from exc
    except places.PlaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Both ends, in two senses. The tiles it left and the tiles it arrived at
    # are both in the scope, and the years it left and the years it arrived in
    # are both re-rendered - otherwise the year it moved out of keeps showing
    # fog that is no longer there.
    _render_views(
        conn,
        sorted(set(_views_for_layers(layers)) | set(_views_for_layers(was))),
        dirty or None,
    )
    _retire_views(before - set(composite.available_views(conn)))
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

    # Deleting can empty a view entirely, so this goes through render_all for
    # the pruning - scoped to the ground the pin actually covered.
    root = tiles_root()
    root.mkdir(parents=True, exist_ok=True)
    composite.render_all(conn, root, scope=composite.rebuild_scope(tiles))
    return {"deleted": place["id"], "name": place["name"]}


@app.get("/api/setup")
def setup_status(
    request: Request, conn: sqlite3.Connection = Depends(get_conn)
) -> dict[str, object]:
    """What first-run setup still needs. Readable without a token.

    The token itself is not, except during genuine first-run setup. Reads are
    open by design, so serving the write token here to anyone who asks handed
    every visitor the ability to change everything - which is not a doorstop,
    it is a lock with the key taped to it.
    """
    from datetime import date

    token, source = effective_token(request)
    completed = tokens.setup_completed(conn)
    reveal = tokens.revealable(conn, source)

    return {
        "version": __version__,
        "completed": completed,
        "token": {
            "source": source,
            # Present only while it is safe to show. Absent is the normal case.
            **({"value": token} if reveal else {}),
        },
        "basemap": basemap.basemap_status(),
        "suggested_urls": basemap.suggested_planet_urls(
            date.today().strftime("%Y%m%d")
        ),
        "data_dir": str(db.data_dir()),
    }


@app.post("/api/setup/complete")
def finish_setup(conn: sqlite3.Connection = Depends(get_conn)) -> dict[str, object]:
    """Mark setup done, which stops the token being served.

    Guarded like every other write, which is exactly the proof required: to
    say you have the token you have to present it. A browser that just read it
    off the setup screen can; a passer-by cannot.
    """
    when = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.transaction(conn):
        tokens.complete_setup(conn, when)
    return {"completed": True, "at": when}


def is_trusted_basemap(url: str) -> bool:
    """Is this one of the published builds Irfaran offers by name?"""
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
    composite.write_placeholders(root, conn)
    rendered = {view: composite.render_view(conn, root, view) for view in views}

    return {
        "events_replayed": replayed,
        "tiles_touched": len(touched),
        "tiles_rendered": rendered,
    }
