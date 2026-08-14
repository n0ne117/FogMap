# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from fogmap import __version__, composite, db
from fogmap.ingest import common, gpx, tcx

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "X-FogMap-Token"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One connection at startup purely to create the schema. Requests open
    # their own, because handlers that do real work run in a worker thread and
    # a SQLite connection belongs to the thread that made it.
    conn = db.open_initialised()
    conn.close()
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
