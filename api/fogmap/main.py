# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Routes only - logic lives in the sibling modules."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from fogmap import __version__, db

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
TOKEN_HEADER = "X-FogMap-Token"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.open_initialised()
    app.state.db = conn
    try:
        yield
    finally:
        conn.close()


app = FastAPI(
    title="FogMap",
    version=__version__,
    summary="Self-hosted fog-of-war location map",
    lifespan=lifespan,
)


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
async def healthz(request: Request) -> dict[str, object]:
    """Liveness plus the one fact worth checking first, the version."""
    conn = request.app.state.db
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "version": __version__}


@app.get("/api/meta")
async def meta(request: Request) -> dict[str, object]:
    """Version, available layers, data extent and cache inventory."""
    conn = request.app.state.db
    return {
        "version": __version__,
        "layers": db.layer_inventory(conn),
        "bbox": None,
        "counts": db.counts(conn),
        "blobs_by_kind": db.blob_counts_by_kind(conn),
        "tiles": 0,
        "settings": db.get_settings(conn),
    }
