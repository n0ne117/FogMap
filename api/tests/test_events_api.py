# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual editing: drawing, erasing and undo."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fogmap import composite, db, geo
from fogmap.ingest import common
from fogmap.main import app, tiles_root

TOKEN = "synthetic-draw-token"

# Open water near Null Island, well away from anywhere real.
LON = 0.62
LAT = 0.30


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
    conn = db.open_initialised()
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM pending_render")
    conn.close()

    with TestClient(app) as test_client:
        yield test_client


def auth(extra: dict | None = None) -> dict:
    return {"X-FogMap-Token": TOKEN, **(extra or {})}


def line(start: float, end: float, lat: float = LAT) -> dict:
    steps = 12
    return {
        "type": "LineString",
        "coordinates": [
            [start + (end - start) * n / steps, lat] for n in range(steps + 1)
        ],
    }


def explored(client, view: str = "all") -> int:
    tile_x, tile_y = geo.lonlat_to_tile(LON, LAT)
    response = client.get(f"/api/tiles/dark/{view}/fog/14/{tile_x}/{tile_y}.png")
    assert response.status_code == 200
    pixels = np.array(Image.open(io.BytesIO(response.content)))
    return int((pixels[..., 3] == 0).sum())


class TestDrawing:
    def test_a_stroke_becomes_an_event_and_appears_on_the_map(self, client):
        assert explored(client) == 0

        response = client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.002)},
        )
        assert response.status_code == 201
        assert response.json()["layers"] == ["prehistory"]
        assert explored(client) > 0

    def test_drawing_needs_the_token(self, client):
        response = client.post(
            "/api/events",
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.001)},
        )
        assert response.status_code == 401

    def test_a_year_range_is_written_to_every_year_it_covers(self, client):
        response = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON, LON + 0.002),
                "layers": ["1994..1997"],
            },
        )
        assert response.json()["layers"] == ["1994", "1995", "1996", "1997"]
        for year in (1994, 1995, 1996, 1997):
            assert explored(client, f"year:{year}") > 0
        assert explored(client, "year:1998") == 0

    def test_a_backwards_range_is_refused_with_a_readable_message(self, client):
        response = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON, LON + 0.001),
                "layers": ["2002..1994"],
            },
        )
        assert response.status_code == 400
        assert "runs backwards" in response.json()["detail"]

    @pytest.mark.parametrize(
        "payload,expected",
        [
            ({"source": "strava"}, "Unknown source"),
            ({"op": "smudge"}, "Unknown op"),
            ({"geometry": "a line"}, "geometry must be a GeoJSON"),
            ({"radius_m": 0}, "radius_m must be greater than 0"),
        ],
    )
    def test_bad_input_is_refused_by_name(self, client, payload, expected):
        body = {
            "source": "manual",
            "op": "add",
            "geometry": line(LON, LON + 0.001),
            **payload,
        }
        response = client.post("/api/events", headers=auth(), json=body)
        assert response.status_code == 400
        assert expected in response.json()["detail"]


class TestErasing:
    def test_an_erase_cuts_a_gap_in_every_view(self, client):
        client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON, LON + 0.004),
                "layers": ["2001..2002"],
            },
        )
        before = explored(client)

        client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "erase",
                "geometry": line(LON + 0.0018, LON + 0.0022),
            },
        )

        # The erase was drawn without a year, but applies to both of them.
        assert explored(client) < before
        for view in ("all", "year:2001", "year:2002"):
            assert explored(client, view) < before

    def test_an_erase_is_stored_against_every_layer(self, client):
        response = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "erase",
                "geometry": line(LON, LON + 0.001),
                "layers": ["2024"],
            },
        )
        # The requested layer is ignored: erase is not a per-year correction.
        assert response.json()["layers"] == ["*"]


class TestUndo:
    def test_deleting_a_stroke_removes_it_from_the_map(self, client):
        created = client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.002)},
        ).json()
        assert explored(client) > 0

        response = client.delete(f"/api/events/{created['id']}", headers=auth())
        assert response.status_code == 200

        # The regression this guards: rendering used to leave the tiles of a
        # deleted stroke on disk, so undo looked like it had done nothing.
        assert explored(client) == 0

    def test_deleting_an_erase_restores_the_fog_underneath(self, client):
        client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.004)},
        )
        whole = explored(client)

        erase = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "erase",
                "geometry": line(LON + 0.0018, LON + 0.0022),
            },
        ).json()
        assert explored(client) < whole

        client.delete(f"/api/events/{erase['id']}", headers=auth())
        assert explored(client) == whole

    def test_undo_leaves_neighbouring_work_alone(self, client):
        far = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON + 0.05, LON + 0.052, lat=LAT + 0.05),
            },
        ).json()
        near = client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.002)},
        ).json()

        conn = db.connect()
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'fog'"
            ).fetchone()["n"]
        finally:
            conn.close()

        client.delete(f"/api/events/{near['id']}", headers=auth())

        conn = db.connect()
        try:
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'fog'"
            ).fetchone()["n"]
            remaining = conn.execute("SELECT id FROM events").fetchall()
        finally:
            conn.close()

        assert [row["id"] for row in remaining] == [far["id"]]
        assert after < before

    def test_deleting_something_that_is_not_there(self, client):
        response = client.delete("/api/events/424242", headers=auth())
        assert response.status_code == 404
        assert "No event with id 424242" in response.json()["detail"]

    def test_deleting_needs_the_token(self, client):
        assert client.delete("/api/events/1").status_code == 401


class TestListing:
    def test_events_come_back_newest_first(self, client):
        for _ in range(3):
            client.post(
                "/api/events",
                headers=auth(),
                json={
                    "source": "manual",
                    "op": "add",
                    "geometry": line(LON, LON + 0.001),
                },
            )
        body = client.get("/api/events").json()
        assert body["total"] == 3
        ids = [event["id"] for event in body["events"]]
        assert ids == sorted(ids, reverse=True)

    def test_filtering_by_source(self, client):
        client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.001)},
        )
        assert client.get("/api/events?source=manual").json()["total"] == 1
        assert client.get("/api/events?source=workout").json()["total"] == 0

    def test_filtering_by_layer(self, client):
        client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON, LON + 0.001),
                "layers": ["1994"],
            },
        )
        assert client.get("/api/events?layer=1994").json()["total"] == 1
        assert client.get("/api/events?layer=1995").json()["total"] == 0

    def test_reading_the_log_needs_no_token(self, client):
        assert client.get("/api/events").status_code == 200


class TestLayerExpansion:
    def test_a_plain_year_is_left_alone(self):
        assert common.expand_layers(["2024"]) == ["2024"]

    def test_a_range_becomes_every_year_in_it(self):
        assert common.expand_layers(["1994..1996"]) == ["1994", "1995", "1996"]

    def test_nothing_given_means_prehistory(self):
        assert common.expand_layers(None) == ["prehistory"]
        assert common.expand_layers([]) == ["prehistory"]
        assert common.expand_layers(["  "]) == ["prehistory"]

    def test_ranges_and_years_can_be_mixed_and_are_deduplicated(self):
        assert common.expand_layers(["1994..1996", "1995", "2024"]) == [
            "1994",
            "1995",
            "1996",
            "2024",
        ]

    def test_an_absurd_range_is_refused(self):
        with pytest.raises(ValueError, match="almost certainly a typo"):
            common.expand_layers(["1000..2024"])

    def test_a_range_that_is_not_years_is_refused(self):
        with pytest.raises(ValueError, match="must be two years"):
            common.expand_layers(["spring..summer"])


class TestStalePruning:
    def test_rendering_removes_tiles_that_no_longer_have_data(self, client):
        created = client.post(
            "/api/events",
            headers=auth(),
            json={"source": "manual", "op": "add", "geometry": line(LON, LON + 0.002)},
        ).json()

        tile_x, tile_y = geo.lonlat_to_tile(LON, LAT)
        rendered = composite.tile_path(
            tiles_root(), "dark", "prehistory", "fog", 14, tile_x, tile_y
        )
        assert rendered.is_file()

        client.delete(f"/api/events/{created['id']}", headers=auth())
        assert not rendered.exists()

    def test_a_view_that_empties_completely_is_removed(self, client):
        created = client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "geometry": line(LON, LON + 0.002),
                "layers": ["1994"],
            },
        ).json()
        assert (tiles_root() / "dark" / "year-1994").is_dir()

        client.delete(f"/api/events/{created['id']}", headers=auth())
        assert not (tiles_root() / "dark" / "year-1994").exists()


class TestDeferredRender:
    """Bulk import: stamp every file, render the lot once.

    Rendering costs roughly the whole archive rather than the file just added,
    so paying it per file is what made importing a few hundred workouts take
    longer than an afternoon. Deferring writes down what is owed, and one call
    settles it.
    """

    def document(self, offset: float) -> bytes:
        from . import synthetic

        points = [
            (synthetic.BASE_LON + offset + n * 0.0002, synthetic.BASE_LAT)
            for n in range(30)
        ]
        return synthetic.gpx_document(points, name=f"track {offset}")

    def upload(self, client, offset: float, defer: bool):
        return client.post(
            f"/api/ingest/gpx?defer_render={'true' if defer else 'false'}",
            headers=auth(),
            files={"file": (f"t{offset}.gpx", self.document(offset), "application/gpx+xml")},
        )

    def test_a_deferred_import_writes_no_tiles_and_records_the_debt(self, client):
        response = self.upload(client, 0.0, defer=True)
        assert response.status_code == 200
        body = response.json()
        assert body["events_created"] == 1
        assert body["render_pending"] is True

        status = client.get("/api/render").json()
        assert status["pending_tiles"] > 0
        assert "all" in status["views"]

    def settle(self, client) -> list[dict]:
        """Run the render and return every progress line it reported."""
        response = client.post("/api/render", headers=auth())
        assert response.status_code == 200
        return [
            json.loads(line)
            for line in response.text.splitlines()
            if line.strip()
        ]

    def test_rendering_settles_the_whole_batch_at_once(self, client):
        for offset in (0.0, 0.05, 0.1):
            assert self.upload(client, offset, defer=True).status_code == 200

        owed = client.get("/api/render").json()["pending_tiles"]
        assert owed >= 1

        steps = self.settle(client)
        assert steps[-1]["pending_tiles"] == owed
        assert steps[-1]["finished"] is True

        # Paid off, and the tiles are on disk.
        assert client.get("/api/render").json()["pending_tiles"] == 0
        assert list((tiles_root() / "dark" / "all" / "fog").glob("14/*/*.png"))

    def test_progress_is_reported_while_it_works(self, client):
        for offset in (0.0, 0.05, 0.1):
            assert self.upload(client, offset, defer=True).status_code == 200

        steps = self.settle(client)
        assert len(steps) >= 3, "no progress was reported, only a result"

        # Starts at nothing, never goes backwards, ends complete.
        assert steps[0]["done"] == 0
        assert steps[0]["total"] > 0
        assert [step["done"] for step in steps] == sorted(
            step["done"] for step in steps
        )
        assert steps[-1]["done"] == steps[-1]["total"]

    def test_rendering_with_nothing_owed_is_a_no_op(self, client):
        steps = self.settle(client)
        assert steps[-1]["pending_tiles"] == 0
        assert steps[-1]["total"] == 0

    def test_a_deferred_import_matches_an_immediate_one(self, client, tmp_path):
        """Deferring must change when tiles are written, not what they are."""
        assert self.upload(client, 0.0, defer=False).status_code == 200
        immediate = sorted(
            (path.relative_to(tiles_root()), path.read_bytes())
            for path in tiles_root().rglob("dark/all/**/*.png")
        )

        conn = db.open_initialised()
        conn.execute("DELETE FROM blobs")
        conn.execute("DELETE FROM events")
        conn.execute("DELETE FROM pending_render")
        conn.close()
        import shutil

        shutil.rmtree(tiles_root(), ignore_errors=True)

        assert self.upload(client, 0.0, defer=True).status_code == 200
        assert self.settle(client)[-1]["finished"] is True
        deferred = sorted(
            (path.relative_to(tiles_root()), path.read_bytes())
            for path in tiles_root().rglob("dark/all/**/*.png")
        )

        assert immediate and immediate == deferred

    def test_the_render_endpoint_needs_the_token(self, client):
        assert client.post("/api/render").status_code == 401
