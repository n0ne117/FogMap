# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workout trackers, with intervals.icu stubbed out.

No test here talks to intervals.icu. What is checked is everything Irfaran is
responsible for: that the request carries the auth the service documents, that
an activity already imported by hand is recognised rather than drawn twice,
that a session with no GPS is counted and skipped rather than failing a sync,
and that the API key never comes back out of the server.

The live handshake is the one part these cannot cover - that needs a real key
against the real service.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from irfaran import db, trackers
from irfaran.ingest import common, gpx
from irfaran.main import app

from . import synthetic

TOKEN = "synthetic-tracker-token"
KEY = "test-api-key-1234"


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    connection = db.open_initialised()
    connection.execute("DELETE FROM events")
    connection.execute("DELETE FROM blobs")
    connection.execute("DELETE FROM pending_render")
    connection.execute("DELETE FROM settings WHERE key LIKE 'intervals%'")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


class FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeService:
    """Stands in for intervals.icu, at the transport.

    Deliberately replaces urlopen rather than trackers.fetch, so every test
    below runs through the real auth header, the real URL building and the real
    mapping of an HTTP status onto a message somebody can act on. Stubbing
    fetch would have skipped exactly the code most likely to be wrong.
    """

    def __init__(self, activities, gpx_for=None, status=None):
        self.activities = activities
        self.gpx_for = gpx_for or {}
        self.status = status or {}
        self.requests: list[urllib.request.Request] = []

    @property
    def paths(self) -> list[str]:
        return [r.full_url.replace(trackers.BASE_URL, "") for r in self.requests]

    def header(self, name: str) -> str:
        return self.requests[0].get_header(name, "") if self.requests else ""

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        path = request.full_url.replace(trackers.BASE_URL, "")

        if path in self.status:
            raise urllib.error.HTTPError(
                request.full_url, self.status[path], "stubbed", {}, None
            )

        if "/activities" in path:
            return FakeResponse(json.dumps(self.activities).encode())

        for identifier, document in self.gpx_for.items():
            if path == f"/activity/{identifier}.gpx":
                return FakeResponse(document.encode())

        # No GPX for this activity: what an indoor session looks like.
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)


def serve(monkeypatch, service: FakeService) -> FakeService:
    monkeypatch.setattr(trackers.urllib.request, "urlopen", service)
    return service


def a_track(hour: int = 6, day: int = 1) -> str:
    """A synthetic GPX document, timestamped so dedup has something to work on.

    The timestamp is the dedup key, so two calls wanting to be different
    activities have to differ here.
    """
    return synthetic.gpx_document(
        synthetic.square_loop(20),
        start=datetime(2026, 8, day, hour, 0, 0, tzinfo=timezone.utc),
    )


# ------------------------------------------------------------------------ auth


class TestAuthentication:
    def test_basic_auth_uses_the_literal_username(self) -> None:
        """intervals.icu wants API_KEY as the username, not the athlete."""
        header = trackers._auth_header(KEY)
        assert header.startswith("Basic ")
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        assert decoded == f"API_KEY:{KEY}"

    def test_the_key_is_never_returned_by_the_settings_endpoint(self, client) -> None:
        client.patch("/api/trackers/intervals", headers=auth(), json={"api_key": KEY})
        body = client.get("/api/settings").json()
        assert "intervals_api_key" not in body["settings"]
        assert KEY not in client.get("/api/settings").text

    def test_the_key_is_never_returned_by_the_trackers_endpoint(self, client) -> None:
        client.patch("/api/trackers/intervals", headers=auth(), json={"api_key": KEY})
        response = client.get("/api/trackers")
        assert KEY not in response.text
        assert response.json()["trackers"][0]["key_set"] is True


# --------------------------------------------------------------------- syncing


class TestSync:
    def test_an_activity_is_imported(self, conn, monkeypatch) -> None:
        service = FakeService(
            [{"id": "i1000"}], gpx_for={"i1000": a_track(6, 1)}
        )
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1
        assert result.events > 0
        assert result.tiles

    def test_the_same_activity_twice_is_not_drawn_twice(self, conn, monkeypatch) -> None:
        service = FakeService(
            [{"id": "i1000"}], gpx_for={"i1000": a_track(6, 1)}
        )
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        trackers.sync(conn, "intervals")
        before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        again = trackers.sync(conn, "intervals")
        after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

        assert after == before
        assert again.imported == 0
        assert again.already_here == 1

    def test_a_workout_imported_by_hand_is_recognised(self, conn, monkeypatch) -> None:
        """The whole reason these are filed under `workout` and not `intervals`."""
        document = a_track(7, 2)
        common.ingest_tracks(conn, "workout", gpx.parse(document))
        before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        assert before > 0

        service = FakeService([{"id": "i2000"}], gpx_for={"i2000": document})
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        result = trackers.sync(conn, "intervals")
        after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        assert after == before
        assert result.already_here == 1
        assert result.imported == 0

    def test_an_activity_with_no_gps_is_counted_not_failed(self, conn, monkeypatch) -> None:
        """A winter of indoor sessions must not look like a broken sync."""
        service = FakeService([{"id": "trainer1"}], gpx_for={})
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        result = trackers.sync(conn, "intervals")
        assert result.no_gps == 1
        assert result.failed == 0

    def test_one_unreadable_file_does_not_stop_the_rest(self, conn, monkeypatch) -> None:
        service = FakeService(
            [{"id": "bad"}, {"id": "good"}],
            gpx_for={"bad": "this is not gpx at all", "good": a_track()},
        )
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1
        assert result.failed + result.no_gps == 1

    def test_a_sync_defers_the_render_rather_than_doing_it(self, conn, monkeypatch) -> None:
        service = FakeService([{"id": "i1"}], gpx_for={"i1": a_track()})
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        trackers.sync(conn, "intervals")
        assert db.pending_render(conn), "the tiles should be owing a render"

    def test_no_key_is_refused_with_something_actionable(self, conn) -> None:
        trackers.put(conn, "intervals", "enabled", "true")
        with pytest.raises(trackers.TrackerError, match="No intervals.icu API key"):
            trackers.sync(conn, "intervals")

    def test_switched_off_is_refused(self, conn) -> None:
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "false")
        with pytest.raises(trackers.TrackerError, match="switched off"):
            trackers.sync(conn, "intervals")

    def test_the_listing_always_carries_an_oldest_date(self, conn, monkeypatch) -> None:
        """Leaving it off is a 422 from the service, not a default of all time."""
        service = FakeService([], gpx_for={})
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        trackers.sync(conn, "intervals")
        listing = [path for path in service.paths if "activities" in path]
        assert listing and "oldest=" in listing[0]

    def test_an_unknown_tracker_is_refused(self, conn) -> None:
        with pytest.raises(trackers.TrackerError, match="Unknown workout tracker"):
            trackers.sync(conn, "strava")


# ----------------------------------------------------------------------- timer


class TestDue:
    def test_not_due_when_switched_off(self, conn) -> None:
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "false")
        assert trackers.is_due(conn, "intervals") is False

    def test_not_due_without_a_key(self, conn) -> None:
        trackers.put(conn, "intervals", "enabled", "true")
        assert trackers.is_due(conn, "intervals") is False

    def test_due_when_it_has_never_run(self, conn) -> None:
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")
        assert trackers.is_due(conn, "intervals") is True

    def test_zero_hours_means_manual_only(self, conn) -> None:
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")
        trackers.put(conn, "intervals", "sync_hours", "0")
        assert trackers.is_due(conn, "intervals") is False

    def test_not_due_again_straight_after_a_run(self, conn, monkeypatch) -> None:
        service = FakeService([], gpx_for={})
        serve(monkeypatch, service)
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")

        trackers.sync(conn, "intervals")
        assert trackers.is_due(conn, "intervals") is False

    def test_a_garbled_timestamp_means_due(self, conn) -> None:
        """Better to check again than to never check because of a bad row."""
        trackers.put(conn, "intervals", "api_key", KEY)
        trackers.put(conn, "intervals", "enabled", "true")
        trackers.put(conn, "intervals", "last_sync", "not a date")
        assert trackers.is_due(conn, "intervals") is True


# ------------------------------------------------------------------- endpoints


class TestEndpoints:
    def test_listing_needs_no_token(self, client) -> None:
        assert client.get("/api/trackers").status_code == 200

    def test_changing_settings_needs_the_token(self, client) -> None:
        refused = client.patch("/api/trackers/intervals", json={"enabled": "true"})
        assert refused.status_code in (401, 403)

    def test_an_unknown_tracker_is_a_404(self, client) -> None:
        response = client.patch(
            "/api/trackers/strava", headers=auth(), json={"enabled": "true"}
        )
        assert response.status_code == 404

    def test_an_unknown_field_is_refused(self, client) -> None:
        response = client.patch(
            "/api/trackers/intervals", headers=auth(), json={"password": "x"}
        )
        assert response.status_code == 400
        assert "Settable" in response.json()["detail"]

    def test_a_blank_key_does_not_wipe_the_stored_one(self, client) -> None:
        """The field is blank on every page load, because the server will not
        say what the key is. Saving anything else must not clear it."""
        client.patch("/api/trackers/intervals", headers=auth(), json={"api_key": KEY})
        client.patch(
            "/api/trackers/intervals", headers=auth(), json={"api_key": "", "enabled": "true"}
        )
        assert client.get("/api/trackers").json()["trackers"][0]["key_set"] is True

    def test_a_non_numeric_interval_is_refused(self, client) -> None:
        response = client.patch(
            "/api/trackers/intervals", headers=auth(), json={"sync_hours": "often"}
        )
        assert response.status_code == 400

    def test_sync_without_a_key_is_a_502_that_says_why(self, client) -> None:
        client.patch("/api/trackers/intervals", headers=auth(), json={"enabled": "true"})
        response = client.post("/api/trackers/intervals/sync", headers=auth())
        assert response.status_code == 502
        assert "API key" in response.json()["detail"]

    def test_a_failed_sync_is_remembered(self, client) -> None:
        client.patch("/api/trackers/intervals", headers=auth(), json={"enabled": "true"})
        client.post("/api/trackers/intervals/sync", headers=auth())
        assert client.get("/api/trackers").json()["trackers"][0]["last_error"]
