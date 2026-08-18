# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reporting progress while a drawn stroke is rasterised.

A stroke is not instant: it is rasterised into every view it belongs to, and on
a full archive that is several seconds during which the only sign of life was
the preview refusing to disappear. The endpoint reports as it goes when asked.

Asked, not always - `POST /api/events` answers a single JSON object as it always
has, because that is what the tests, the places page and anything else built on
it expect. Only `?progress=1` streams.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from irfaran import db
from irfaran.main import app

TOKEN = "synthetic-draw-progress-token"

STROKE = {
    "source": "manual",
    "op": "add",
    "radius_m": 20,
    "geometry": {
        "type": "LineString",
        "coordinates": [[11.11, 44.44], [11.1105, 44.4405], [11.111, 44.441]],
    },
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


def lines(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


class TestThePlainResponse:
    def test_it_is_still_one_json_object(self, client) -> None:
        response = client.post("/api/events", headers=auth(), json=STROKE)
        assert response.status_code == 201
        body = response.json()
        assert isinstance(body, dict)
        assert body["id"] and body["layers"]

    def test_it_carries_no_progress_fields(self, client) -> None:
        body = client.post("/api/events", headers=auth(), json=STROKE).json()
        assert "done" not in body
        assert "finished" not in body


class TestTheStreamedResponse:
    def test_it_reports_progress_then_the_result(self, client) -> None:
        response = client.post("/api/events?progress=1", headers=auth(), json=STROKE)
        assert response.status_code == 201

        reported = lines(response)
        assert len(reported) >= 2, "a stream with no progress in it is not a stream"
        assert reported[-1]["finished"] is True
        assert all("done" in step and "total" in step for step in reported)

    def test_the_last_line_says_everything_the_plain_one_would(self, client) -> None:
        plain = client.post("/api/events", headers=auth(), json=STROKE).json()
        streamed = lines(
            client.post("/api/events?progress=1", headers=auth(), json=STROKE)
        )[-1]
        for key in ("op", "layers", "radius_m", "tiles_touched"):
            assert streamed[key] == plain[key], f"{key} differs between the two paths"

    def test_progress_never_goes_backwards(self, client) -> None:
        reported = lines(
            client.post("/api/events?progress=1", headers=auth(), json=STROKE)
        )
        done = [step["done"] for step in reported]
        assert done == sorted(done)
        assert done[-1] == reported[-1]["total"]

    def test_the_event_is_stored_exactly_once(self, client) -> None:
        before = _count()
        client.post("/api/events?progress=1", headers=auth(), json=STROKE)
        assert _count() == before + 1

    def test_the_tiles_are_rendered_by_the_time_it_finishes(self, client) -> None:
        """Not deferred. A stroke that needs a second request to appear would
        vanish if the browser were closed in between."""
        response = client.post("/api/events?progress=1", headers=auth(), json=STROKE)
        assert lines(response)[-1]["tiles_touched"] > 0
        conn = db.connect()
        try:
            assert not db.pending_render(conn), "the stroke was left owing a render"
        finally:
            conn.close()

    def test_a_bad_stroke_is_refused_before_any_streaming(self, client) -> None:
        """An error has to be a status code, not a line inside a 201."""
        response = client.post(
            "/api/events?progress=1",
            headers=auth(),
            json={**STROKE, "geometry": {"type": "Rhombus", "coordinates": []}},
        )
        assert response.status_code == 400
        assert "detail" in response.json()

    def test_it_still_needs_the_token(self, client) -> None:
        response = client.post("/api/events?progress=1", json=STROKE)
        assert response.status_code in (401, 403)


def _count() -> int:
    conn = db.connect()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE source = 'manual'"
            ).fetchone()["n"]
        )
    finally:
        conn.close()
