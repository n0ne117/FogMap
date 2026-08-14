# SPDX-License-Identifier: AGPL-3.0-or-later
"""The tile endpoint and the PMTiles range endpoint."""

from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fogmap import composite, db
from fogmap.ingest import common, gpx
from fogmap.main import app, tiles_root

from . import synthetic


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def rendered(client):
    """Import a synthetic track and render its pyramid into the data dir."""
    conn = db.connect()
    try:
        conn.execute("DELETE FROM blobs")
        conn.execute("DELETE FROM events")
        document = synthetic.gpx_document(synthetic.square_loop(40))
        common.ingest_tracks(conn, "workout", gpx.parse(document))
        root = tiles_root()
        composite.write_placeholders(root)
        composite.render_view(conn, root, "all")
    finally:
        conn.close()
    return client


def image_of(response) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(response.content)))


class TestTileEndpoint:
    def test_a_rendered_tile_comes_back_as_a_png(self, rendered):
        response = rendered.get("/api/tiles/dark/all/fog/0/0/0.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert image_of(response).shape == (256, 256, 4)

    def test_tiles_carry_a_cache_header(self, rendered):
        response = rendered.get("/api/tiles/dark/all/fog/0/0/0.png")
        assert "max-age" in response.headers["cache-control"]

    def test_ground_nobody_has_visited_comes_back_as_solid_fog(self, client):
        # Not a 404. Unvisited ground is not missing, it is unexplored, and
        # a hole here would clear the fog over most of the world.
        response = client.get("/api/tiles/dark/all/fog/14/1/1.png")
        assert response.status_code == 200
        assert (image_of(response)[..., 3] == composite.fog_alpha()).all()

    def test_an_unvisited_trail_tile_is_transparent(self, client):
        response = client.get("/api/tiles/dark/all/trail/14/1/1.png")
        assert response.status_code == 200
        assert (image_of(response)[..., 3] == 0).all()

    @pytest.mark.parametrize("theme", ["light", "dark"])
    def test_both_themes_are_served(self, rendered, theme):
        assert rendered.get(f"/api/tiles/{theme}/all/fog/0/0/0.png").status_code == 200

    def test_the_themes_return_different_pixels(self, rendered):
        dark = image_of(rendered.get("/api/tiles/dark/all/fog/0/0/0.png"))
        light = image_of(rendered.get("/api/tiles/light/all/fog/0/0/0.png"))
        assert not np.array_equal(dark, light)
        # Same shape, different colour - the fog is in the same place.
        assert np.array_equal(dark[..., 3], light[..., 3])

    def test_an_unknown_theme_is_refused(self, client):
        response = client.get("/api/tiles/sepia/all/fog/0/0/0.png")
        assert response.status_code == 404
        assert "Unknown theme" in response.json()["detail"]

    def test_an_unknown_kind_is_refused(self, client):
        response = client.get("/api/tiles/dark/all/clouds/0/0/0.png")
        assert response.status_code == 404
        assert "Unknown tile kind" in response.json()["detail"]

    def test_reading_a_tile_needs_no_token(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        assert client.get("/api/tiles/dark/all/fog/0/0/0.png").status_code == 200


class TestBasemapEndpoint:
    def test_a_missing_archive_says_what_to_do_about_it(self, client):
        response = client.get("/api/basemap/planet.pmtiles")
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert "Download a Protomaps PMTiles archive" in detail

    def test_a_path_traversal_attempt_is_refused(self, client):
        assert client.get("/api/basemap/..%2F..%2Fetc%2Fpasswd").status_code == 404

    def test_a_name_that_is_not_pmtiles_is_refused(self, client):
        assert client.get("/api/basemap/fogmap.db").status_code == 404

    def test_a_range_request_returns_only_that_slice(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(bytes(range(256)) * 8)
        try:
            response = client.get(
                "/api/basemap/test.pmtiles", headers={"Range": "bytes=10-19"}
            )
            assert response.status_code == 206
            assert response.content == bytes(range(10, 20))
            assert response.headers["content-range"] == f"bytes 10-19/{256 * 8}"
            assert response.headers["accept-ranges"] == "bytes"
        finally:
            archive.unlink()

    def test_an_open_ended_range_runs_to_the_end_of_the_file(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(b"0123456789")
        try:
            response = client.get(
                "/api/basemap/test.pmtiles", headers={"Range": "bytes=6-"}
            )
            assert response.status_code == 206
            assert response.content == b"6789"
        finally:
            archive.unlink()

    def test_a_suffix_range_returns_the_last_bytes(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(b"0123456789")
        try:
            response = client.get(
                "/api/basemap/test.pmtiles", headers={"Range": "bytes=-3"}
            )
            assert response.status_code == 206
            assert response.content == b"789"
        finally:
            archive.unlink()

    def test_a_range_past_the_end_is_refused_with_416(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(b"0123456789")
        try:
            response = client.get(
                "/api/basemap/test.pmtiles", headers={"Range": "bytes=500-600"}
            )
            assert response.status_code == 416
        finally:
            archive.unlink()

    def test_head_reports_the_size_without_the_body(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(b"0123456789")
        try:
            response = client.head("/api/basemap/test.pmtiles")
            assert response.status_code == 200
            assert response.headers["content-length"] == "10"
            assert response.headers["accept-ranges"] == "bytes"
        finally:
            archive.unlink()

    def test_no_range_header_returns_the_whole_archive(self, client):
        archive = db.data_dir() / "test.pmtiles"
        archive.write_bytes(b"0123456789")
        try:
            response = client.get("/api/basemap/test.pmtiles")
            assert response.status_code == 200
            assert response.content == b"0123456789"
        finally:
            archive.unlink()
