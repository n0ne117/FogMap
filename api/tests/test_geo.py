# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the coordinate math.

Every coordinate used here is either a well-known public landmark or an
obviously synthetic value. No real tracking data appears in this repository.
"""

from __future__ import annotations

import math

import pytest

from fogmap import geo

# St Stephen's Cathedral, Vienna. A public landmark, chosen because the build
# plan quotes its ground resolution.
VIENNA_LON = 16.373819
VIENNA_LAT = 48.208488

ROUND_TRIP_POINTS = [
    (0.0, 0.0),
    (VIENNA_LON, VIENNA_LAT),
    (-74.044502, 40.689247),  # Statue of Liberty
    (151.215256, -33.856159),  # Sydney Opera House
    (-180.0, 0.0),
    (180.0, 0.0),
    (179.999999, 84.0),
    (-12.5, -84.0),
    (0.0, geo.MAX_LAT),
    (0.0, -geo.MAX_LAT),
]


class TestConstants:
    def test_world_is_z14_at_256px_tiles(self):
        assert geo.WORLD_PX == 4_194_304
        assert geo.WORLD_PX == geo.TILE_PX * 2**geo.NATIVE_Z

    def test_metres_per_pixel_at_equator(self):
        assert geo.M_PER_PX_EQ == pytest.approx(9.5546, abs=1e-4)

    def test_mercator_limit(self):
        assert geo.MAX_LAT == pytest.approx(85.0511, abs=1e-4)


class TestProjection:
    def test_null_island_lands_at_the_centre_of_the_world(self):
        x_px, y_px = geo.lonlat_to_px(0.0, 0.0)
        assert x_px == pytest.approx(geo.WORLD_PX / 2)
        assert y_px == pytest.approx(geo.WORLD_PX / 2)

    def test_west_and_north_edges_are_the_origin(self):
        x_px, y_px = geo.lonlat_to_px(-180.0, geo.MAX_LAT)
        assert x_px == pytest.approx(0.0, abs=1e-6)
        assert y_px == pytest.approx(0.0, abs=1e-6)

    def test_east_and_south_edges_are_the_far_corner(self):
        x_px, y_px = geo.lonlat_to_px(180.0, -geo.MAX_LAT)
        assert x_px == pytest.approx(geo.WORLD_PX, abs=1e-6)
        assert y_px == pytest.approx(geo.WORLD_PX, abs=1e-6)

    def test_x_grows_east_and_y_grows_south(self):
        west, _ = geo.lonlat_to_px(-10.0, 0.0)
        east, _ = geo.lonlat_to_px(10.0, 0.0)
        _, north = geo.lonlat_to_px(0.0, 10.0)
        _, south = geo.lonlat_to_px(0.0, -10.0)
        assert west < east
        assert north < south

    @pytest.mark.parametrize("lon,lat", ROUND_TRIP_POINTS)
    def test_round_trip_within_one_micro_degree(self, lon, lat):
        got_lon, got_lat = geo.px_to_lonlat(*geo.lonlat_to_px(lon, lat))
        assert got_lon == pytest.approx(lon, abs=1e-6)
        assert got_lat == pytest.approx(lat, abs=1e-6)


class TestTiles:
    def test_vienna_stephansdom_z14_tile(self):
        assert geo.lonlat_to_tile(VIENNA_LON, VIENNA_LAT) == (8937, 5681)

    def test_null_island_sits_on_the_z14_tile_boundary(self):
        assert geo.lonlat_to_tile(0.0, 0.0) == (8192, 8192)
        assert geo.tile_count(geo.NATIVE_Z) == 16384

    def test_tile_origin_round_trips_through_px_to_tile(self):
        origin = geo.tile_origin_px(8937, 5681)
        assert geo.px_to_tile(*origin) == (8937, 5681)

    def test_a_z14_tile_at_vienna_covers_about_1_6_km(self):
        metres = geo.TILE_PX * geo.m_per_px(VIENNA_LAT)
        assert metres == pytest.approx(1630.0, abs=20.0)

    def test_ancestors_walk_from_z13_to_z0(self):
        chain = geo.ancestors(8937, 5681)
        assert len(chain) == 14
        assert chain[0] == (13, 4468, 2840)
        assert chain[-1] == (0, 0, 0)

    def test_tile_count_rejects_zoom_below_the_deepest_rendered(self):
        assert geo.tile_count(geo.MAX_Z) == 2**geo.MAX_Z
        with pytest.raises(ValueError, match=f"zoom must be between 0 and {geo.MAX_Z}"):
            geo.tile_count(geo.MAX_Z + 1)

    def test_descendants_fill_the_native_tile(self):
        assert geo.descendants(8937, 5681, geo.NATIVE_Z) == [(8937, 5681)]

        one_deeper = geo.descendants(8937, 5681, geo.NATIVE_Z + 1)
        assert len(one_deeper) == 4
        assert (8937 * 2, 5681 * 2) in one_deeper
        assert (8937 * 2 + 1, 5681 * 2 + 1) in one_deeper

        assert len(geo.descendants(8937, 5681, geo.MAX_Z)) == 4 ** (
            geo.MAX_Z - geo.NATIVE_Z
        )

    def test_descendants_refuses_to_look_upwards(self):
        with pytest.raises(ValueError, match="no descendants"):
            geo.descendants(8937, 5681, 13)


class TestDeeperGrids:
    """The stored grid is z14; the PNG pyramid is rendered below it."""

    def test_a_deeper_grid_is_a_power_of_two_finer(self):
        native = geo.lonlat_to_px(VIENNA_LON, VIENNA_LAT)
        deep = geo.lonlat_to_px(VIENNA_LON, VIENNA_LAT, geo.NATIVE_Z + 2)
        assert deep[0] == pytest.approx(native[0] * 4)
        assert deep[1] == pytest.approx(native[1] * 4)

    def test_ground_resolution_halves_with_every_level(self):
        native = geo.m_per_px(VIENNA_LAT)
        assert geo.m_per_px(VIENNA_LAT, geo.NATIVE_Z + 1) == pytest.approx(native / 2)
        assert geo.m_per_px(VIENNA_LAT, geo.MAX_Z) == pytest.approx(native / 4)

    def test_a_brush_covers_more_pixels_on_a_deeper_grid(self):
        native = geo.radius_px(15.0, VIENNA_LAT)
        deep = geo.radius_px(15.0, VIENNA_LAT, geo.MAX_Z)
        assert native < 3.0  # the reason the deep levels exist at all
        assert deep == pytest.approx(native * 4)

    def test_a_deep_tile_sits_inside_its_native_parent(self):
        parent = geo.lonlat_to_tile(VIENNA_LON, VIENNA_LAT)
        deep = geo.lonlat_to_tile(VIENNA_LON, VIENNA_LAT, geo.MAX_Z)
        assert deep in geo.descendants(*parent, geo.MAX_Z)


class TestGroundResolution:
    def test_equator(self):
        assert geo.m_per_px(0.0) == pytest.approx(9.5546, abs=1e-4)

    def test_vienna(self):
        assert geo.m_per_px(48.2) == pytest.approx(6.37, abs=5e-3)

    def test_resolution_shrinks_towards_the_poles(self):
        assert geo.m_per_px(0.0) > geo.m_per_px(48.2) > geo.m_per_px(80.0)

    def test_northern_and_southern_hemispheres_match(self):
        assert geo.m_per_px(48.2) == pytest.approx(geo.m_per_px(-48.2))

    def test_brush_radius_is_wider_in_pixels_nearer_the_poles(self):
        assert geo.radius_px(15.0, 0.0) == pytest.approx(15.0 / 9.5546, rel=1e-4)
        assert geo.radius_px(15.0, VIENNA_LAT) > geo.radius_px(15.0, 0.0)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_brush_radius_rejects_non_positive_metres(self, bad):
        with pytest.raises(ValueError, match="brush radius must be greater than 0 m"):
            geo.radius_px(bad, 0.0)


class TestLatitudeClamp:
    def test_clamps_at_the_mercator_limit(self):
        assert geo.clamp_lat(90.0) == geo.MAX_LAT
        assert geo.clamp_lat(-90.0) == -geo.MAX_LAT
        assert geo.clamp_lat(85.5) == geo.MAX_LAT

    def test_leaves_ordinary_latitudes_alone(self):
        assert geo.clamp_lat(VIENNA_LAT) == VIENNA_LAT
        assert geo.clamp_lat(0.0) == 0.0

    def test_the_poles_stay_inside_the_world_grid(self):
        for lat in (90.0, -90.0, 1e6, -1e6):
            _, y_px = geo.lonlat_to_px(0.0, lat)
            assert 0.0 <= y_px <= geo.WORLD_PX

    def test_projection_never_blows_up_at_the_poles(self):
        _, north = geo.lonlat_to_px(0.0, 90.0)
        _, south = geo.lonlat_to_px(0.0, -90.0)
        assert math.isfinite(north) and math.isfinite(south)


class TestLongitudeWrap:
    @pytest.mark.parametrize(
        "given,expected",
        [
            (0.0, 0.0),
            (180.0, 180.0),
            (-180.0, -180.0),
            (181.0, -179.0),
            (-181.0, 179.0),
            (540.0, -180.0),
            (360.0, 0.0),
        ],
    )
    def test_wraps_into_range(self, given, expected):
        assert geo.wrap_lon(given) == pytest.approx(expected, abs=1e-9)


class TestAntimeridian:
    def test_a_small_step_across_the_date_line_is_detected(self):
        assert geo.crosses_antimeridian(179.9, -179.9) is True
        assert geo.crosses_antimeridian(-179.9, 179.9) is True

    def test_ordinary_movement_is_not_a_crossing(self):
        assert geo.crosses_antimeridian(16.0, 16.5) is False
        assert geo.crosses_antimeridian(-10.0, 10.0) is False

    def test_a_crossing_path_is_split_rather_than_drawn_round_the_world(self):
        path = [(179.8, 0.0), (179.9, 0.0), (-179.9, 0.0), (-179.8, 0.0)]
        segments = geo.split_antimeridian(path)

        assert len(segments) == 2
        assert segments[0] == [(179.8, 0.0), (179.9, 0.0)]
        assert segments[1] == [(-179.9, 0.0), (-179.8, 0.0)]

        # The whole point: no segment spans more than a sliver of the world.
        for segment in segments:
            xs = [geo.lonlat_to_px(lon, lat)[0] for lon, lat in segment]
            assert max(xs) - min(xs) < geo.WORLD_PX / 2

    def test_a_path_that_stays_put_is_returned_whole(self):
        path = [(16.0, 48.0), (16.1, 48.1), (16.2, 48.2)]
        assert geo.split_antimeridian(path) == [path]

    def test_multiple_crossings_produce_multiple_segments(self):
        path = [(179.0, 0.0), (-179.0, 0.0), (179.0, 0.0)]
        assert len(geo.split_antimeridian(path)) == 3

    def test_empty_and_single_point_paths(self):
        assert geo.split_antimeridian([]) == []
        assert geo.split_antimeridian([(1.0, 2.0)]) == [[(1.0, 2.0)]]


class TestBadInput:
    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_coordinates_are_rejected_loudly(self, bad):
        with pytest.raises(ValueError, match="must be a finite number"):
            geo.lonlat_to_px(bad, 0.0)
        with pytest.raises(ValueError, match="must be a finite number"):
            geo.lonlat_to_px(0.0, bad)

    def test_non_numeric_coordinates_name_the_offending_value(self):
        with pytest.raises(ValueError, match="longitude must be a number"):
            geo.lonlat_to_px("sixteen", 48.0)
