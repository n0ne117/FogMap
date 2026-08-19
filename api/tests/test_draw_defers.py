# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drawing hands the rendering to the queue, and says so.

This replaces the tests for `POST /api/events?progress=1`, which streamed the
render back as newline-delimited JSON. Their premise was that deferring would be
worse - "a stroke that needs a second request to appear would vanish if the
browser were closed in between" - and that was true while the only thing able to
render was a request. It stopped being true when the queue moved into the API
process: the render starts here, on the server, and the browser is a spectator.

What has to hold now is that a stroke is stored and owing before the response
comes back, that the queue has been asked to draw it, and that the same panel
which watches an import's render watches this one too.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from irfaran import db, renderq
from irfaran.main import app

TOKEN = "synthetic-draw-defer-token"

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
    conn = db.open_initialised()
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM pending_render")
    conn.execute("DELETE FROM render_done")
    conn.commit()
    conn.close()
    with TestClient(app) as test_client:
        yield test_client
    settle(test_client)


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


def settle(client, timeout: float = 60.0) -> dict:
    """Let the queue finish, so one test's render is not the next one's noise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/render").json()
        if state["state"] not in ("running", "stopping"):
            return state
        time.sleep(0.02)
    raise AssertionError("the queue never went idle")


class TestTheResponse:
    def test_it_is_one_json_object(self, client) -> None:
        response = client.post("/api/events", headers=auth(), json=STROKE)
        assert response.status_code == 201
        body = response.json()
        assert body["id"] and body["layers"]

    def test_it_says_the_render_is_owing(self, client) -> None:
        body = client.post("/api/events", headers=auth(), json=STROKE).json()
        assert body["tiles_touched"] > 0
        assert body["render_pending"] > 0, "a stroke that owes nothing was never drawn"

    def test_a_bad_stroke_is_refused_and_owes_nothing(self, client) -> None:
        before = client.get("/api/render").json()["pending_tiles"]
        response = client.post(
            "/api/events",
            headers=auth(),
            json={**STROKE, "geometry": {"type": "Rhombus", "coordinates": []}},
        )
        assert response.status_code == 400
        assert client.get("/api/render").json()["pending_tiles"] == before

    def test_it_still_needs_the_token(self, client) -> None:
        assert client.post("/api/events", json=STROKE).status_code in (401, 403)


class TestTheQueueTakesIt:
    def test_the_render_is_under_way_without_a_second_request(self, client) -> None:
        """Nobody has to ask. The endpoint starts the queue before answering."""
        client.post("/api/events", headers=auth(), json=STROKE)
        state = client.get("/api/render").json()
        assert state["state"] in ("running", "idle")
        assert state["state"] == "running" or state["pending_tiles"] == 0, (
            "the stroke was left owing a render with nothing running"
        )

    def test_it_finishes_and_settles_the_debt(self, client) -> None:
        client.post("/api/events", headers=auth(), json=STROKE)
        final = settle(client)
        assert final["state"] == "idle"
        assert final["pending_tiles"] == 0
        assert final["tiles_written"] > 0

    def test_undo_defers_too(self, client) -> None:
        drawn = client.post("/api/events", headers=auth(), json=STROKE).json()
        settle(client)

        response = client.delete(f"/api/events/{drawn['id']}", headers=auth())
        assert response.status_code == 200
        state = client.get("/api/render").json()
        assert state["state"] == "running" or state["pending_tiles"] == 0
        assert settle(client)["pending_tiles"] == 0

    def test_only_the_views_the_stroke_belongs_to_are_drawn(self, client) -> None:
        """The economy that makes deferring cheaper than the inline render was.

        Without a recorded view list the queue asks which views hold data in
        these tiles, and every extra view it names costs a shallow pass over
        z0 to z13. A stroke knows its own views, so it says.
        """
        client.post("/api/events", headers=auth(), json=STROKE)
        conn = db.connect()
        try:
            recorded = db.pending_views(conn)
        finally:
            conn.close()
        assert recorded is not None, "the stroke did not record its views"
        assert "all" in recorded
        assert len(recorded) <= 2, f"a single stroke claimed {recorded}"


class TestDrawingWhileItIsBusy:
    def test_a_second_stroke_mid_render_is_not_lost(self, client) -> None:
        """The ordinary case now, and the one that used to drop tiles."""
        client.post("/api/events", headers=auth(), json=STROKE)

        second = {
            **STROKE,
            "geometry": {
                "type": "LineString",
                "coordinates": [[11.5, 44.8], [11.5005, 44.8005], [11.501, 44.801]],
            },
        }
        body = client.post("/api/events", headers=auth(), json=second).json()
        assert body["render_pending"] > 0

        final = settle(client)
        assert final["pending_tiles"] == 0, (
            "tiles owed by a stroke drawn during a render were left owing"
        )
