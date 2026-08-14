# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 8: fog edge softening and the vector trail endpoint."""

from __future__ import annotations

import numpy as np
import pytest
from fastapi.testclient import TestClient

from fogmap import composite, db, geo
from fogmap.ingest import common, gpx
from fogmap.main import TRAIL_FEATURE_CAP, app

from . import synthetic

TILE = geo.TILE_PX
TOKEN = "synthetic-polish-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
    conn = db.open_initialised()
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM events")
    conn.close()
    with TestClient(app) as test_client:
        yield test_client


class TestFogEdge:
    def _scene(self) -> np.ndarray:
        explored = np.zeros((TILE, TILE), dtype=bool)
        rows, cols = np.ogrid[:TILE, :TILE]
        explored |= ((rows - 128) ** 2 + (cols - 128) ** 2) <= 30**2
        return explored

    def test_a_soft_edge_produces_partly_transparent_fog(self):
        rgba = composite.render_fog(self._scene(), "dark", edge_px=3.0)
        alpha = rgba[..., 3]
        assert ((alpha > 0) & (alpha < 255)).any()

    def test_a_hard_edge_produces_none(self):
        rgba = composite.render_fog(self._scene(), "dark", edge_px=0.0)
        assert set(np.unique(rgba[..., 3])) <= {0, 255}

    def test_explored_ground_is_never_dimmed_by_the_fade(self):
        """The fade runs outwards only.

        Blurring the mask itself would leave a one-pixel trail sitting under
        its own haze, which looks worse than no softening at all.
        """
        explored = self._scene()
        for radius in (0.0, 1.5, 3.0, 6.0):
            alpha = composite.render_fog(explored, "dark", edge_px=radius)[..., 3]
            assert (alpha[explored] == 0).all(), f"dimmed at radius {radius}"

    def test_a_one_pixel_trail_still_reads_as_fully_clear(self):
        explored = np.zeros((TILE, TILE), dtype=bool)
        explored[128, :] = True
        alpha = composite.render_fog(explored, "dark", edge_px=4.0)[..., 3]
        assert (alpha[128, :] == 0).all()

    def test_a_wider_edge_softens_more_ground(self):
        explored = self._scene()
        counts = [
            int(
                (
                    (composite.render_fog(explored, "dark", edge_px=r)[..., 3] > 0)
                    & (composite.render_fog(explored, "dark", edge_px=r)[..., 3] < 255)
                ).sum()
            )
            for r in (1.0, 3.0, 6.0)
        ]
        assert counts == sorted(counts)

    def test_ground_nobody_has_visited_stays_solid(self):
        alpha = composite.render_fog(
            np.zeros((TILE, TILE), dtype=bool), "dark", edge_px=4.0
        )[..., 3]
        assert (alpha == 255).all()

    def test_the_radius_is_configurable(self, monkeypatch):
        monkeypatch.setenv("FOGMAP_FOG_EDGE_PX", "7")
        assert composite.fog_edge_px() == 7.0

    def test_zero_turns_softening_off(self, monkeypatch):
        monkeypatch.setenv("FOGMAP_FOG_EDGE_PX", "0")
        assert composite.fog_edge_px() == 0.0

    def test_a_nonsense_radius_is_refused_loudly(self, monkeypatch):
        monkeypatch.setenv("FOGMAP_FOG_EDGE_PX", "soft")
        with pytest.raises(ValueError, match="FOGMAP_FOG_EDGE_PX must be a number"):
            composite.fog_edge_px()


class TestTrailsEndpoint:
    @pytest.fixture
    def seeded(self, client):
        conn = db.connect()
        try:
            for day in (1, 2, 3):
                from datetime import datetime, timezone

                document = synthetic.gpx_document(
                    synthetic.straight_line(30),
                    name=f"run {day}",
                    start=datetime(2024, 3, day, 9, 0, tzinfo=timezone.utc),
                )
                common.ingest_tracks(conn, "workout", gpx.parse(document))
        finally:
            conn.close()
        return client

    def test_tracks_in_the_viewport_come_back_as_geojson(self, seeded):
        body = seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26").json()

        assert body["type"] == "FeatureCollection"
        assert len(body["features"]) == 3
        assert body["features"][0]["geometry"]["type"] == "LineString"

    def test_features_carry_what_a_click_needs_to_identify_them(self, seeded):
        feature = seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26").json()["features"][0]
        properties = feature["properties"]

        assert properties["source"] == "workout"
        assert properties["layers"] == ["2024"]
        assert properties["meta"]["track"].startswith("run ")
        assert properties["radius_m"] == 20.0

    def test_tracks_outside_the_viewport_are_left_out(self, seeded):
        body = seeded.get("/api/trails?bbox=10.0,10.0,10.5,10.5").json()
        assert body["features"] == []

    def test_filtering_by_layer(self, seeded):
        assert len(seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26&layer=2024").json()["features"]) == 3
        assert seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26&layer=1999").json()["features"] == []

    def test_the_response_is_capped(self, seeded):
        body = seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26").json()
        assert body["cap"] == TRAIL_FEATURE_CAP
        assert body["truncated"] is False

    def test_asking_for_the_whole_world_is_refused(self, seeded):
        """Section 1: nothing may scale with point count except this, and
        only because the viewport bounds it."""
        response = seeded.get("/api/trails?bbox=-180,-85,180,85")
        assert response.status_code == 400
        assert "degrees across" in response.json()["detail"]

    @pytest.mark.parametrize(
        "bbox,expected",
        [
            ("1,2,3", "west,south,east,north"),
            ("a,b,c,d", "must be numbers"),
            ("5,5,1,1", "inside out"),
        ],
    )
    def test_a_bad_bbox_is_refused_by_name(self, seeded, bbox, expected):
        response = seeded.get(f"/api/trails?bbox={bbox}")
        assert response.status_code == 400
        assert expected in response.json()["detail"]

    def test_reading_trails_needs_no_token(self, seeded):
        assert seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26").status_code == 200

    def test_erase_events_are_not_offered_as_trails(self, seeded):
        seeded.post(
            "/api/events",
            headers={"X-FogMap-Token": TOKEN},
            json={
                "source": "manual",
                "op": "erase",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[0.5, 0.25], [0.501, 0.25]],
                },
            },
        )
        body = seeded.get("/api/trails?bbox=0.49,0.24,0.52,0.26").json()
        assert all(f["properties"]["source"] != "manual" for f in body["features"])
