# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live tracking: Overland, OwnTracks and Home Assistant.

Every source is off by default, so most of what matters here is what happens
when they are switched off, and what happens when a phone that has been in a
tunnel finally delivers what it recorded.
"""

from __future__ import annotations

import io
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fogmap import db, geo
from fogmap.ingest import live
from fogmap.main import app

TOKEN = "synthetic-live-token"

# Open water near Null Island.
LON, LAT = 0.90, 0.60
STEP = 0.00012
START = datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
    conn = db.open_initialised()
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM events")
    for source in live.LIVE_SOURCES:
        live.set_enabled(conn, source, False)
    conn.close()

    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict:
    return {"X-FogMap-Token": TOKEN}


def enable(client, source: str) -> None:
    response = client.patch(
        "/api/settings",
        headers=auth(),
        json={live.setting_key(source): "true"},
    )
    assert response.status_code == 200


def feature(index: int, accuracy: float = 8.0) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [LON + index * STEP, LAT]},
        "properties": {
            "timestamp": (START + timedelta(seconds=30 * index)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "horizontal_accuracy": accuracy,
            "motion": ["walking"],
            "device_id": "synthetic-phone",
        },
    }


def explored(client, index: int = 20) -> int:
    tile_x, tile_y = geo.lonlat_to_tile(LON + index * STEP, LAT)
    response = client.get(f"/api/tiles/dark/all/fog/14/{tile_x}/{tile_y}.png")
    pixels = np.array(Image.open(io.BytesIO(response.content)))
    return int((pixels[..., 3] == 0).sum())


class TestOffByDefault:
    @pytest.mark.parametrize("source", live.LIVE_SOURCES)
    def test_every_source_starts_switched_off(self, client, source):
        body = client.get("/api/settings").json()
        state = next(s for s in body["sources"] if s["source"] == source)
        assert state["enabled"] is False

    @pytest.mark.parametrize("source", live.LIVE_SOURCES)
    def test_a_switched_off_endpoint_refuses_politely(self, client, source):
        response = client.post(
            f"/api/ingest/{source}", headers=auth(), json={"locations": []}
        )
        assert response.status_code == 503
        detail = response.json()["detail"]
        assert "switched off" in detail
        assert "Nothing was recorded" in detail

    def test_the_app_works_with_every_source_off(self, client):
        for path in ("/healthz", "/api/meta", "/api/places", "/api/settings"):
            assert client.get(path).status_code == 200
        assert client.get("/api/tiles/dark/all/fog/2/2/1.png").status_code == 200

    def test_a_source_is_hidden_until_it_is_on_or_has_data(self, client):
        sources = live.LIVE_SOURCES
        conn = db.connect()
        try:
            assert not any(live.has_events(conn, source) for source in sources)
        finally:
            conn.close()

        enable(client, "overland")
        body = client.get("/api/settings").json()
        shown = [s for s in body["sources"] if s["enabled"] or s["has_events"]]
        assert [s["source"] for s in shown] == ["overland"]


class TestOverland:
    def test_a_batch_lands_and_clears_fog(self, client):
        enable(client, "overland")
        assert explored(client) == 0

        response = client.post(
            "/api/ingest/overland",
            headers=auth(),
            json={"locations": [feature(i) for i in range(15)]},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 15
        assert explored(client) > 0

    def test_overlands_own_bearer_token_is_accepted(self, client):
        enable(client, "overland")
        response = client.post(
            "/api/ingest/overland",
            headers={"Authorization": f"Bearer {TOKEN}"},
            json={"locations": [feature(0)]},
        )
        assert response.status_code == 200

    def test_a_wrong_bearer_token_is_refused(self, client):
        enable(client, "overland")
        response = client.post(
            "/api/ingest/overland",
            headers={"Authorization": "Bearer nonsense"},
            json={"locations": [feature(0)]},
        )
        assert response.status_code == 401

    def test_no_credentials_at_all_is_refused(self, client):
        enable(client, "overland")
        response = client.post(
            "/api/ingest/overland", json={"locations": [feature(0)]}
        )
        assert response.status_code == 401

    def test_batches_that_arrive_out_of_order_still_make_one_track(self, client):
        enable(client, "overland")

        # The phone was in a tunnel and delivers the later stretch first.
        client.post(
            "/api/ingest/overland",
            headers=auth(),
            json={"locations": [feature(i) for i in range(20, 30)]},
        )
        client.post(
            "/api/ingest/overland",
            headers=auth(),
            json={"locations": [feature(i) for i in range(0, 20)]},
        )

        events = client.get("/api/events?source=overland").json()
        assert events["total"] == 1

        conn = db.connect()
        try:
            row = conn.execute(
                "SELECT geometry FROM events WHERE source = 'overland'"
            ).fetchone()
        finally:
            conn.close()

        import json as _json

        coordinates = _json.loads(row["geometry"])["coordinates"]
        longitudes = [pair[0] for pair in coordinates]
        # Sorted by time, so the track runs in one direction rather than
        # doubling back on itself where the batches were joined.
        assert longitudes == sorted(longitudes)
        assert len(coordinates) == 30

    def test_a_replayed_batch_changes_nothing(self, client):
        enable(client, "overland")
        batch = {"locations": [feature(i) for i in range(10)]}

        client.post("/api/ingest/overland", headers=auth(), json=batch)
        before = explored(client, 5)

        again = client.post("/api/ingest/overland", headers=auth(), json=batch)
        assert again.json() == {
            "accepted": 0,
            "duplicates": 10,
            "dropped": 0,
            "event_id": None,
            "tiles_touched": 0,
        }
        assert explored(client, 5) == before

    def test_one_event_per_day_not_one_per_fix(self, client):
        enable(client, "overland")
        for index in range(0, 30, 10):
            client.post(
                "/api/ingest/overland",
                headers=auth(),
                json={"locations": [feature(i) for i in range(index, index + 10)]},
            )

        events = client.get("/api/events?source=overland").json()
        assert events["total"] == 1
        assert events["events"][0]["meta"]["fixes"] == 30

    def test_a_batch_spanning_midnight_becomes_two_tracks(self, client):
        enable(client, "overland")
        late = dict(feature(0))
        late["properties"] = {
            **late["properties"],
            "timestamp": "2026-08-14T23:58:00Z",
        }
        early = dict(feature(1))
        early["properties"] = {
            **early["properties"],
            "timestamp": "2026-08-15T00:02:00Z",
        }

        client.post(
            "/api/ingest/overland", headers=auth(), json={"locations": [late, early]}
        )
        assert client.get("/api/events?source=overland").json()["total"] == 2

    def test_inaccurate_fixes_are_dropped_server_side(self, client):
        enable(client, "overland")
        response = client.post(
            "/api/ingest/overland",
            headers=auth(),
            json={
                "locations": [feature(0, accuracy=8), feature(1, accuracy=250)]
            },
        )
        body = response.json()
        assert body["accepted"] == 1
        assert body["dropped"] == 1

    def test_motion_is_kept(self, client):
        enable(client, "overland")
        client.post(
            "/api/ingest/overland", headers=auth(), json={"locations": [feature(0)]}
        )
        events = client.get("/api/events?source=overland").json()
        assert events["events"][0]["meta"]["motion"] == ["walking"]

    def test_a_large_batch_is_accepted(self, client):
        enable(client, "overland")
        response = client.post(
            "/api/ingest/overland",
            headers=auth(),
            json={"locations": [feature(i) for i in range(600)]},
        )
        assert response.json()["accepted"] == 600

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"nope": []}, 'Overland payloads look like'),
            ({"locations": "x"}, "must be a list"),
            ({"locations": [{"geometry": {}}]}, "no [lon, lat] coordinates"),
        ],
    )
    def test_malformed_batches_are_refused_by_name(self, client, payload, expected):
        enable(client, "overland")
        response = client.post("/api/ingest/overland", headers=auth(), json=payload)
        assert response.status_code == 400
        assert expected in response.json()["detail"]


class TestOwnTracks:
    def test_a_location_report_lands(self, client):
        enable(client, "owntracks")
        response = client.post(
            "/api/ingest/owntracks",
            headers=auth(),
            json={
                "_type": "location",
                "lat": LAT,
                "lon": LON,
                "acc": 12,
                "tst": int(START.timestamp()),
                "tid": "ax",
            },
        )
        assert response.json()["accepted"] == 1

    def test_a_zero_length_body_is_accepted_quietly(self, client):
        # OwnTracks posts an empty body when a friend is deleted. Refusing it
        # would fill its logs with errors over nothing.
        enable(client, "owntracks")
        response = client.post(
            "/api/ingest/owntracks", headers=auth(), content=b""
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 0

    def test_a_zero_length_body_still_respects_the_toggle(self, client):
        response = client.post(
            "/api/ingest/owntracks", headers=auth(), content=b""
        )
        assert response.status_code == 503

    @pytest.mark.parametrize("kind", ["transition", "waypoint", "card", "cmd"])
    def test_other_message_types_are_ignored(self, client, kind):
        enable(client, "owntracks")
        response = client.post(
            "/api/ingest/owntracks", headers=auth(), json={"_type": kind}
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 0

    def test_user_and_device_headers_are_kept(self, client):
        enable(client, "owntracks")
        client.post(
            "/api/ingest/owntracks",
            headers={**auth(), "X-Limit-U": "alex", "X-Limit-D": "iphone"},
            json={
                "_type": "location",
                "lat": LAT,
                "lon": LON,
                "tst": int(START.timestamp()),
            },
        )
        meta = client.get("/api/events?source=owntracks").json()["events"][0]["meta"]
        assert meta["user"] == "alex"
        assert meta["device"] == "iphone"

    def test_a_unix_timestamp_is_understood(self, client):
        enable(client, "owntracks")
        client.post(
            "/api/ingest/owntracks",
            headers=auth(),
            json={
                "_type": "location",
                "lat": LAT,
                "lon": LON,
                "tst": int(START.timestamp()),
            },
        )
        assert client.get("/api/events?source=owntracks").json()["events"][0][
            "layers"
        ] == ["2026"]


class TestHomeAssistant:
    def test_a_fix_lands(self, client):
        enable(client, "ha")
        response = client.post(
            "/api/ingest/ha",
            headers=auth(),
            json={
                "lat": LAT,
                "lon": LON,
                "accuracy": 18,
                "timestamp": "2026-08-14T10:15:00+00:00",
                "device": "device_tracker.my_phone",
            },
        )
        assert response.json()["accepted"] == 1

    @pytest.mark.parametrize("accuracy", ["unknown", "", None])
    def test_a_missing_accuracy_is_treated_as_unknown_not_an_error(
        self, client, accuracy
    ):
        # Home Assistant sends the string "unknown" when the attribute is not
        # populated yet, which used to crash the append.
        enable(client, "ha")
        response = client.post(
            "/api/ingest/ha",
            headers=auth(),
            json={
                "lat": LAT,
                "lon": LON,
                "accuracy": accuracy,
                "timestamp": "2026-08-14T10:15:00+00:00",
            },
        )
        assert response.status_code == 200
        assert response.json()["accepted"] == 1

    def test_a_second_fix_appends_to_the_same_track(self, client):
        """The first fix is a Point and the second makes it a LineString.

        Reading that Point back as a list of pairs is what broke here.
        """
        enable(client, "ha")
        for offset in range(3):
            response = client.post(
                "/api/ingest/ha",
                headers=auth(),
                json={
                    "lat": LAT,
                    "lon": LON + offset * STEP,
                    "accuracy": 18,
                    "timestamp": (START + timedelta(minutes=offset)).isoformat(),
                },
            )
            assert response.status_code == 200

        events = client.get("/api/events?source=ha").json()
        assert events["total"] == 1
        assert events["events"][0]["meta"]["fixes"] == 3


class TestSettings:
    def test_toggling_needs_the_token(self, client):
        assert client.patch(
            "/api/settings", json={"overland_ingest_enabled": "true"}
        ).status_code == 401

    def test_a_toggle_survives_being_read_back(self, client):
        enable(client, "overland")
        settings = client.get("/api/settings").json()["settings"]
        assert settings["overland_ingest_enabled"] == "true"
        assert settings["owntracks_ingest_enabled"] == "false"

    def test_turning_a_source_off_again_refuses_new_fixes(self, client):
        enable(client, "overland")
        assert (
            client.post(
                "/api/ingest/overland",
                headers=auth(),
                json={"locations": [feature(0)]},
            ).status_code
            == 200
        )

        client.patch(
            "/api/settings",
            headers=auth(),
            json={"overland_ingest_enabled": "false"},
        )
        assert (
            client.post(
                "/api/ingest/overland",
                headers=auth(),
                json={"locations": [feature(1)]},
            ).status_code
            == 503
        )

    def test_an_empty_patch_is_refused(self, client):
        response = client.patch("/api/settings", headers=auth(), json={})
        assert response.status_code == 400


class TestRebuildStaysCanonical:
    def test_a_live_track_rebuilds_to_the_same_bytes(self, client):
        """Invariant 1 holds for the live path too.

        Appending rebuilds the affected tiles from the event log rather than
        stamping the new stretch on top, so what a live day produces is what a
        full rebuild produces.
        """
        from fogmap import raster

        enable(client, "overland")
        for index in range(0, 30, 10):
            client.post(
                "/api/ingest/overland",
                headers=auth(),
                json={"locations": [feature(i) for i in range(index, index + 10)]},
            )

        conn = db.connect()
        try:
            before = {
                (r["kind"], r["source"], r["layer"], r["x"], r["y"]): bytes(r["data"])
                for r in conn.execute("SELECT * FROM blobs")
            }
            raster.rebuild(conn)
            after = {
                (r["kind"], r["source"], r["layer"], r["x"], r["y"]): bytes(r["data"])
                for r in conn.execute("SELECT * FROM blobs")
            }
        finally:
            conn.close()

        assert before == after
