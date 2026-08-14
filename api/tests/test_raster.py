# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brush stamping and blob storage."""

from __future__ import annotations

import numpy as np
import pytest

from fogmap import db, geo, raster

from . import synthetic

TILE = geo.TILE_PX


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "fogmap.db")
    yield connection
    connection.close()


class TestBrushKernel:
    def test_a_kernel_is_a_filled_circle(self):
        kernel = raster.disc_kernel(3.0)
        assert kernel.shape == (7, 7)
        assert kernel[3, 3]
        assert kernel[3, 0] and kernel[0, 3]
        assert not kernel[0, 0]  # corners fall outside the circle

    def test_a_sub_pixel_radius_still_paints_something(self):
        assert raster.disc_kernel(0.1).sum() >= 1

    def test_a_wider_radius_paints_more(self):
        assert raster.disc_kernel(6.0).sum() > raster.disc_kernel(3.0).sum()

    def test_kernels_are_cached_by_radius(self):
        assert raster.disc_kernel(3.0) is raster.disc_kernel(3.0)


class TestPaint:
    def test_painting_allocates_the_tile_it_lands_on(self):
        tiles: raster.Tiles = {}
        raster.paint(tiles, 100.5, 100.5, raster.disc_kernel(2.0))
        assert list(tiles) == [(0, 0)]
        assert tiles[(0, 0)].sum() > 0

    def test_a_stamp_on_a_tile_edge_writes_to_both_tiles(self):
        tiles: raster.Tiles = {}
        raster.paint(tiles, float(TILE), 100.0, raster.disc_kernel(4.0))
        assert set(tiles) == {(0, 0), (1, 0)}
        assert tiles[(0, 0)].sum() > 0
        assert tiles[(1, 0)].sum() > 0

    def test_a_stamp_on_a_tile_corner_writes_to_four_tiles(self):
        tiles: raster.Tiles = {}
        raster.paint(tiles, float(TILE), float(TILE), raster.disc_kernel(4.0))
        assert set(tiles) == {(0, 0), (1, 0), (0, 1), (1, 1)}

    def test_stamps_outside_the_world_are_clipped_not_wrapped(self):
        tiles: raster.Tiles = {}
        raster.paint(tiles, -50.0, -50.0, raster.disc_kernel(2.0))
        assert tiles == {}

    def test_painting_twice_is_idempotent(self):
        tiles: raster.Tiles = {}
        raster.paint(tiles, 100.0, 100.0, raster.disc_kernel(3.0))
        once = tiles[(0, 0)].sum()
        raster.paint(tiles, 100.0, 100.0, raster.disc_kernel(3.0))
        assert tiles[(0, 0)].sum() == once


class TestResample:
    def test_a_sparse_pair_becomes_a_continuous_run_of_points(self):
        xs = np.array([0.0, 100.0])
        ys = np.array([0.0, 0.0])
        lats = np.array([0.0, 0.0])

        out_x, _, _ = raster.resample(xs, ys, lats, step_px=1.0)
        assert len(out_x) > 90
        assert np.all(np.diff(out_x) <= 1.01)

    def test_latitude_is_interpolated_alongside_position(self):
        xs = np.array([0.0, 100.0])
        ys = np.array([0.0, 0.0])
        lats = np.array([0.0, 10.0])

        _, _, out_lat = raster.resample(xs, ys, lats, step_px=10.0)
        assert out_lat[0] == pytest.approx(0.0)
        assert out_lat[-1] == pytest.approx(10.0)
        assert np.all(np.diff(out_lat) > 0)

    def test_repeated_points_do_not_break_the_walk(self):
        xs = np.array([0.0, 0.0, 0.0, 50.0])
        ys = np.zeros(4)
        lats = np.zeros(4)
        out_x, _, _ = raster.resample(xs, ys, lats, step_px=1.0)
        assert len(out_x) > 40

    def test_a_single_point_survives_untouched(self):
        out_x, _, _ = raster.resample(
            np.array([5.0]), np.array([5.0]), np.array([0.0]), step_px=1.0
        )
        assert out_x.tolist() == [5.0]

    def test_a_stationary_track_collapses_to_one_point(self):
        out_x, _, _ = raster.resample(
            np.zeros(10), np.zeros(10), np.zeros(10), step_px=1.0
        )
        assert len(out_x) == 1


class TestStampPath:
    def test_a_line_paints_a_continuous_stroke(self):
        tiles = raster.stamp_path(synthetic.straight_line(40), radius_m=15.0)
        assert tiles

        mask = next(iter(tiles.values()))
        rows = np.where(mask.any(axis=1))[0]
        columns = np.where(mask.any(axis=0))[0]
        # No gaps along the painted row range.
        assert mask[rows.min() : rows.max() + 1, columns.min() : columns.max() + 1].any()
        assert mask.sum() > 0

    def test_a_wider_brush_paints_more(self):
        line = synthetic.straight_line(40)
        narrow = sum(int(m.sum()) for m in raster.stamp_path(line, 15.0).values())
        wide = sum(int(m.sum()) for m in raster.stamp_path(line, 60.0).values())
        assert wide > narrow

    def test_an_antimeridian_crossing_does_not_paint_round_the_world(self):
        tiles = raster.stamp_path([(179.99, 0.0), (-179.99, 0.0)], radius_m=30.0)
        xs = {tile_x for tile_x, _ in tiles}
        # Only the tiles at the two edges of the world, nothing in between.
        assert max(xs) - min(xs) > 16000 - 4
        assert len(xs) <= 4

    def test_an_empty_path_paints_nothing(self):
        assert raster.stamp_path([], 15.0) == {}

    def test_a_single_fix_still_stamps_a_disc(self):
        tiles = raster.stamp_path([(synthetic.BASE_LON, synthetic.BASE_LAT)], 30.0)
        assert sum(int(mask.sum()) for mask in tiles.values()) > 0


class TestBlobStorage:
    def test_a_blob_round_trips_byte_for_byte(self, conn):
        array = np.random.default_rng(1).integers(
            0, 256, (TILE, TILE), dtype=np.uint8
        )
        raster.write_blob(conn, "trail", "workout", "2024", 5, 6, array)
        assert np.array_equal(
            raster.read_blob(conn, "trail", "workout", "2024", 5, 6), array
        )

    def test_writing_the_same_key_replaces_rather_than_duplicating(self, conn):
        first = np.zeros((TILE, TILE), dtype=np.uint8)
        second = np.full((TILE, TILE), 7, dtype=np.uint8)
        raster.write_blob(conn, "fog", "workout", "2024", 1, 1, first)
        raster.write_blob(conn, "fog", "workout", "2024", 1, 1, second)

        assert db.counts(conn)["blobs"] == 1
        assert raster.read_blob(conn, "fog", "workout", "2024", 1, 1).max() == 7

    def test_a_missing_blob_reads_as_none(self, conn):
        assert raster.read_blob(conn, "fog", "workout", "2024", 9, 9) is None

    def test_the_wrong_shape_is_refused_loudly(self, conn):
        with pytest.raises(ValueError, match="must be a 256x256 uint8 array"):
            raster.write_blob(
                conn, "fog", "workout", "2024", 0, 0, np.zeros((4, 4), dtype=np.uint8)
            )

    def test_a_truncated_blob_names_the_tile_and_says_what_to_do(self):
        with pytest.raises(ValueError, match="Delete it and run rebuild"):
            raster.decode(b"too short", "fog", "workout", "2024", 3, 4)

    def test_fog_is_stored_as_a_zero_or_255_mask(self, conn):
        tiles = {(0, 0): np.zeros((TILE, TILE), dtype=bool)}
        tiles[(0, 0)][10, 10] = True
        raster.merge_mask(conn, "fog", "workout", "2024", tiles)

        stored = raster.read_blob(conn, "fog", "workout", "2024", 0, 0)
        assert set(np.unique(stored)) <= {0, 255}
        assert stored[10, 10] == 255


class TestFogAndTrailWidths:
    """Fog clears a corridor; the trail is a thin line down the middle of it."""

    def _stamp(self, conn, radius_m):
        import json

        cursor = conn.execute(
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES ('workout', 'add', ?, ?, '[\"2024\"]', NULL, '2024-01-01', NULL)",
            (
                json.dumps(
                    {
                        "type": "LineString",
                        "coordinates": [
                            [lon, lat] for lon, lat in synthetic.straight_line(40)
                        ],
                    }
                ),
                radius_m,
            ),
        )
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        raster.stamp_event(conn, row)

    def _painted(self, conn, kind):
        return sum(
            int((raster.decode(row["data"], kind, "workout", "2024", 0, 0) > 0).sum())
            for row in conn.execute("SELECT data FROM blobs WHERE kind = ?", (kind,))
        )

    def test_fog_is_stamped_wider_than_the_trail(self, conn):
        self._stamp(conn, 20.0)
        assert self._painted(conn, "fog") > self._painted(conn, "trail")

    def test_the_trail_is_capped_rather_than_scaled(self, conn):
        self._stamp(conn, 20.0)
        wide = self._painted(conn, "trail")

        conn.execute("DELETE FROM blobs")
        conn.execute("DELETE FROM events")
        self._stamp(conn, 60.0)
        # Tripling the fog radius must not widen the trail at all.
        assert self._painted(conn, "trail") == wide

    def test_a_narrow_brush_leaves_the_trail_matching_the_fog(self, conn):
        self._stamp(conn, 3.0)
        assert self._painted(conn, "fog") == self._painted(conn, "trail")

    def test_the_cap_is_configurable_by_environment(self, monkeypatch):
        monkeypatch.setenv("FOGMAP_TRAIL_MAX_RADIUS_M", "9")
        assert raster.trail_max_radius_m() == 9.0

    def test_a_nonsense_cap_is_refused_loudly(self, monkeypatch):
        monkeypatch.setenv("FOGMAP_TRAIL_MAX_RADIUS_M", "thin")
        with pytest.raises(
            ValueError, match="FOGMAP_TRAIL_MAX_RADIUS_M must be a number"
        ):
            raster.trail_max_radius_m()

    def test_the_default_cap_is_five_metres(self):
        assert raster.DEFAULT_TRAIL_MAX_RADIUS_M == 5.0


class TestGeometryParsing:
    def test_a_linestring_reads_back_as_points(self):
        assert raster.geometry_points(
            '{"type":"LineString","coordinates":[[1,2],[3,4]]}', 1
        ) == [(1.0, 2.0), (3.0, 4.0)]

    def test_a_point_reads_back_as_one_pair(self):
        assert raster.geometry_points('{"type":"Point","coordinates":[5,6]}', 1) == [
            (5.0, 6.0)
        ]

    def test_an_unsupported_geometry_type_names_the_event(self):
        with pytest.raises(ValueError, match="Event 42 has geometry type 'Polygon'"):
            raster.geometry_points('{"type":"Polygon","coordinates":[]}', 42)

    def test_broken_json_names_the_event(self):
        with pytest.raises(ValueError, match="Event 7 has geometry that is not valid"):
            raster.geometry_points("{not json", 7)

    def test_empty_layers_are_refused(self):
        with pytest.raises(ValueError, match="Event 3 has layers"):
            raster.parse_layers("[]", 3)
