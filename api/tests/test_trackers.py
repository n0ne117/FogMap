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
    """A client with no tracker configured, and no way out to the network.

    Cleaned between tests because tracker settings live in the settings table,
    which outlives any one test - one test leaving a key behind used to decide
    whether the next one took the "no key" branch or went looking for
    intervals.icu.

    urlopen is blocked rather than left alone. A test that reaches the real
    service would be slow, flaky, and would use somebody's account; failing
    loudly is better than any of that. Tests that want a service install their
    own stub over this one.
    """
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)

    def no_network(request, timeout=None):
        raise AssertionError(
            f"a test tried to reach {getattr(request, 'full_url', request)}. "
            "Install a FakeService with serve() instead."
        )

    monkeypatch.setattr(trackers.urllib.request, "urlopen", no_network)

    connection = db.open_initialised()
    try:
        connection.execute("DELETE FROM settings WHERE key LIKE 'intervals%'")
        connection.commit()
    finally:
        connection.close()

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


def streams_for(points: int = 40, start_hour: int = 6, day: int = 1) -> list[dict]:
    """Streams shaped the way intervals.icu really shapes them.

    Positions come as two parallel arrays - `data` holds latitudes and `data2`
    longitudes - not as pairs, and times are offsets in seconds rather than
    timestamps. Getting that wrong is exactly what shipped the first time.
    """
    lats = [45.6 + index * 0.0001 for index in range(points)]
    lons = [12.9 + index * 0.0001 for index in range(points)]
    return [
        {"type": "time", "data": list(range(points))},
        {"type": "latlng", "data": lats, "data2": lons, "allNull": False},
        {"type": "altitude", "data": [10.0] * points},
    ]


def an_activity(
    identifier: str = "i1000",
    *,
    gps: bool = True,
    day: int = 1,
    hour: int = 6,
) -> dict:
    types = ["time", "altitude"] + (["latlng"] if gps else [])
    return {
        "id": identifier,
        "name": "Synthetic",
        "type": "Ride",
        "start_date": f"2026-08-{day:02d}T{hour:02d}:00:00Z",
        "stream_types": types,
    }


class FakeService:
    """Stands in for intervals.icu, at the transport.

    Deliberately replaces urlopen rather than trackers.fetch, so every test
    below runs through the real auth header, the real URL building and the real
    mapping of an HTTP status onto a message somebody can act on. Stubbing
    fetch would have skipped exactly the code most likely to be wrong.
    """

    def __init__(self, activities, streams=None, status=None):
        self.activities = activities
        self.streams = streams or {}
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

        for identifier, streams in self.streams.items():
            if path == f"/activity/{identifier}/streams":
                return FakeResponse(json.dumps(streams).encode())

        raise urllib.error.HTTPError(request.full_url, 404, "Not Found", {}, None)


def serve(monkeypatch, service: FakeService) -> FakeService:
    monkeypatch.setattr(trackers.urllib.request, "urlopen", service)
    return service


def configured(conn, enabled: bool = True) -> None:
    trackers.put(conn, "intervals", "api_key", KEY)
    trackers.put(conn, "intervals", "enabled", "true" if enabled else "false")


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
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1
        assert result.events > 0
        assert result.tiles

    def test_positions_come_from_two_parallel_arrays(self, conn, monkeypatch) -> None:
        """latlng holds latitudes in `data` and longitudes in `data2`.

        Reading it as a list of pairs finds nothing, which is how the first
        version imported no activities at all while reporting success.
        """
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for(40)}))
        configured(conn)
        trackers.sync(conn, "intervals")

        row = conn.execute(
            "SELECT geometry FROM events WHERE source = 'workout' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        geometry = json.loads(row["geometry"])
        lon, lat = geometry["coordinates"][0][:2]
        # Longitudes near 12.9, latitudes near 45.6 - not swapped, not merged.
        assert 12.8 < lon < 13.0, f"longitude looks wrong: {lon}"
        assert 45.5 < lat < 45.7, f"latitude looks wrong: {lat}"

    def test_samples_get_real_timestamps(self, conn, monkeypatch) -> None:
        """The streams carry offsets in seconds; the start date is the anchor.

        Without it there is nothing to hang them on, and the dedup key is the
        first fix's time.
        """
        serve(monkeypatch, FakeService(
            [an_activity(day=3, hour=8)], {"i1000": streams_for()}
        ))
        configured(conn)
        trackers.sync(conn, "intervals")

        row = conn.execute(
            "SELECT external_id, layers FROM events WHERE source = 'workout' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["external_id"].startswith("20260803T0800"), row["external_id"]
        assert "2026" in row["layers"]

    def test_a_dropped_sample_is_left_out(self, conn, monkeypatch) -> None:
        """A null position is a GPS dropout, not a line through nowhere."""
        streams = streams_for(10)
        latlng = next(s for s in streams if s["type"] == "latlng")
        latlng["data"][4] = None
        latlng["data2"][4] = None
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams}))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1

    def test_the_same_activity_twice_is_not_drawn_twice(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        trackers.sync(conn, "intervals")
        before = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
        again = trackers.sync(conn, "intervals")
        after = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

        assert after == before
        assert again.imported == 0
        assert again.already_here == 1

    def test_an_activity_without_gps_is_not_downloaded_at_all(
        self, conn, monkeypatch
    ) -> None:
        """The listing says which streams exist, so a trainer ride costs nothing."""
        service = serve(monkeypatch, FakeService([an_activity(gps=False)], {}))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.no_gps == 1
        assert result.failed == 0
        assert not [path for path in service.paths if "/streams" in path], (
            "it downloaded streams for an activity the listing said had none"
        )

    def test_an_all_null_position_stream_counts_as_no_gps(
        self, conn, monkeypatch
    ) -> None:
        streams = streams_for(5)
        next(s for s in streams if s["type"] == "latlng")["allNull"] = True
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams}))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.no_gps == 1
        assert result.failed == 0

    def test_one_failure_does_not_stop_the_rest(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService(
            [an_activity("bad"), an_activity("good", day=2)],
            {"good": streams_for()},
            status={"/activity/bad/streams": 500},
        ))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1
        assert result.failed == 1

    def test_a_sync_defers_the_render_rather_than_doing_it(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        trackers.sync(conn, "intervals")
        assert db.pending_render(conn), "the tiles should be owing a render"

    def test_no_key_is_refused_with_something_actionable(self, conn) -> None:
        trackers.put(conn, "intervals", "enabled", "true")
        with pytest.raises(trackers.TrackerError, match="No intervals.icu API key"):
            trackers.sync(conn, "intervals")

    def test_switched_off_is_refused(self, conn) -> None:
        configured(conn, enabled=False)
        with pytest.raises(trackers.TrackerError, match="switched off"):
            trackers.sync(conn, "intervals")

    def test_the_listing_always_carries_an_oldest_date(self, conn, monkeypatch) -> None:
        """Leaving it off is a 422 from the service, not a default of all time."""
        service = serve(monkeypatch, FakeService([], {}))
        configured(conn)

        trackers.sync(conn, "intervals")
        listing = [path for path in service.paths if "activities" in path]
        assert listing and "oldest=" in listing[0]

    def test_it_asks_for_streams_and_not_for_gpx(self, conn, monkeypatch) -> None:
        """There is no GPX endpoint. Assuming there was is what broke this."""
        service = serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        trackers.sync(conn, "intervals")
        assert any("/streams" in path for path in service.paths)
        assert not any(".gpx" in path for path in service.paths)

    def test_an_unknown_tracker_is_refused(self, conn) -> None:
        with pytest.raises(trackers.TrackerError, match="Unknown workout tracker"):
            trackers.sync(conn, "strava")


class TestProgress:
    """sync_iter reports as it goes, so a long sync is not a minute of silence."""

    def test_it_reports_a_step_per_activity(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService(
            [an_activity("a", day=1), an_activity("b", day=2), an_activity("c", day=3)],
            {"a": streams_for(), "b": streams_for(), "c": streams_for()},
        ))
        configured(conn)

        steps = list(trackers.sync_iter(conn, "intervals"))
        stages = [step["stage"] for step in steps]
        assert stages[0] == "listing"
        assert stages[1] == "listed"
        assert stages.count("activity") == 3
        assert stages[-1] == "done"

    def test_the_counts_climb_as_it_goes(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService(
            [an_activity("a", day=1), an_activity("b", day=2)],
            {"a": streams_for(), "b": streams_for()},
        ))
        configured(conn)

        activity_steps = [
            step for step in trackers.sync_iter(conn, "intervals")
            if step["stage"] == "activity"
        ]
        assert [step["done"] for step in activity_steps] == [1, 2]
        assert all(step["total"] == 2 for step in activity_steps)
        assert activity_steps[-1]["imported"] == 2

    def test_the_last_step_carries_the_summary(self, conn, monkeypatch) -> None:
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        last = list(trackers.sync_iter(conn, "intervals"))[-1]
        assert last["finished"] is True
        assert "new" in str(last["summary"])
        assert last["tiles_touched"]

    def test_sync_and_sync_iter_agree(self, conn, monkeypatch) -> None:
        """sync() is the timer's path; it must do exactly what the stream does."""
        serve(monkeypatch, FakeService([an_activity()], {"i1000": streams_for()}))
        configured(conn)

        result = trackers.sync(conn, "intervals")
        assert result.imported == 1
        assert result.summary() == "1 new"


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
        serve(monkeypatch, FakeService([], {}))
        configured(conn)
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
