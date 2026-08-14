# SPDX-License-Identifier: AGPL-3.0-or-later
"""Colourising, PNG encoding and the tile pyramid on disk."""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image

from fogmap import composite, db, geo
from fogmap.ingest import common, gpx

from . import synthetic

TILE = geo.TILE_PX


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "fogmap.db")
    yield connection
    connection.close()


@pytest.fixture
def seeded(conn):
    document = synthetic.gpx_document(synthetic.square_loop(40))
    common.ingest_tracks(conn, "workout", gpx.parse(document))
    return conn


class TestFogRendering:
    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_explored_ground_is_transparent(self, theme):
        fog = np.zeros((TILE, TILE), dtype=bool)
        fog[10, 10] = True
        rgba = composite.render_fog(fog, theme)
        assert rgba[10, 10, 3] == 0

    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_unexplored_ground_is_opaque(self, theme):
        rgba = composite.render_fog(np.zeros((TILE, TILE), dtype=bool), theme)
        assert (rgba[..., 3] == 255).all()

    def test_the_two_themes_differ_in_colour_but_not_in_shape(self):
        fog = np.zeros((TILE, TILE), dtype=bool)
        fog[0:50, 0:50] = True
        dark = composite.render_fog(fog, "dark")
        light = composite.render_fog(fog, "light")

        assert np.array_equal(dark[..., 3], light[..., 3])
        assert not np.array_equal(dark[..., :3], light[..., :3])

    def test_an_unknown_theme_is_refused_by_name(self):
        with pytest.raises(ValueError, match="Unknown theme 'sepia'"):
            composite.render_fog(np.zeros((TILE, TILE), dtype=bool), "sepia")


class TestTrailColourmap:
    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_never_crossed_is_fully_transparent(self, theme):
        assert composite.trail_lut(theme)[0].tolist() == [0, 0, 0, 0]

    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_one_pass_is_already_clearly_visible(self, theme):
        assert composite.trail_lut(theme)[1][3] > 150

    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_alpha_never_decreases_as_passes_accumulate(self, theme):
        alpha = composite.trail_lut(theme)[1:, 3].astype(int)
        assert (np.diff(alpha) >= 0).all()

    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_a_frequent_route_is_distinguishable_from_a_single_pass(self, theme):
        lut = composite.trail_lut(theme)
        assert not np.array_equal(lut[1], lut[40])

    def test_the_ramp_is_walked_on_a_log_scale(self):
        # A linear ramp would put the midpoint near 128. Counts are skewed
        # towards 1, so the midpoint has to sit far lower to be useful.
        lut = composite.trail_lut("dark")
        midpoint = lut[:, :3].sum(axis=1)
        halfway = int(np.argmin(np.abs(midpoint - midpoint[255] / 2)))
        assert halfway < 60

    def test_the_table_covers_every_possible_count(self):
        assert composite.trail_lut("dark").shape == (256, 4)


class TestPngEncoding:
    def test_a_tile_round_trips_as_rgba(self):
        rgba = composite.render_fog(np.zeros((TILE, TILE), dtype=bool), "dark")
        decoded = Image.open(io.BytesIO(composite.encode_png(rgba)))

        assert decoded.mode == "RGBA"
        assert decoded.size == (TILE, TILE)
        assert np.array_equal(np.array(decoded), rgba)

    def test_a_placeholder_is_solid_fog(self):
        decoded = np.array(
            Image.open(io.BytesIO(composite.placeholder_tile("dark", "fog")))
        )
        assert (decoded[..., 3] == 255).all()

    def test_a_placeholder_trail_is_entirely_transparent(self):
        decoded = np.array(
            Image.open(io.BytesIO(composite.placeholder_tile("dark", "trail")))
        )
        assert (decoded[..., 3] == 0).all()


class TestTilePaths:
    def test_a_year_view_loses_the_colon(self, tmp_path):
        path = composite.tile_path(tmp_path, "dark", "year:2024", "fog", 14, 1, 2)
        assert "year-2024" in str(path)
        assert ":" not in str(path)

    def test_the_path_follows_theme_view_kind_z_x_y(self, tmp_path):
        path = composite.tile_path(tmp_path, "light", "all", "trail", 9, 42, 7)
        assert path.relative_to(tmp_path).parts == (
            "light",
            "all",
            "trail",
            "9",
            "42",
            "7.png",
        )


class TestPyramid:
    def test_folding_z14_upwards_reaches_a_single_root(self):
        levels = composite.pyramid_levels({(8937, 5681)})
        assert levels[14] == {(8937, 5681)}
        assert levels[0] == {(0, 0)}
        assert len(levels) == 15

    def test_neighbours_converge_as_the_pyramid_narrows(self):
        levels = composite.pyramid_levels({(100, 100), (101, 100)})
        assert len(levels[14]) == 2
        assert len(levels[0]) == 1

    def test_rendering_writes_every_zoom_for_both_themes(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        written = composite.render_view(seeded, root, "all")

        assert written > 0
        for zoom in (0, 7, 14):
            assert list(root.glob(f"dark/all/fog/{zoom}/*/*.png"))
            assert list(root.glob(f"light/all/fog/{zoom}/*/*.png"))
            assert list(root.glob(f"dark/all/trail/{zoom}/*/*.png"))

    def test_the_root_tile_exists_and_is_a_valid_png(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all")

        z0 = root / "dark" / "all" / "fog" / "0" / "0" / "0.png"
        assert z0.is_file()
        assert Image.open(z0).size == (TILE, TILE)

    def test_an_empty_view_writes_nothing(self, conn, tmp_path):
        assert composite.render_view(conn, tmp_path / "tiles", "all") == 0

    def test_rendering_is_reproducible(self, seeded, tmp_path):
        first = tmp_path / "a"
        second = tmp_path / "b"
        composite.render_view(seeded, first, "all")
        composite.render_view(seeded, second, "all")

        left = sorted(first.rglob("*.png"))
        right = sorted(second.rglob("*.png"))
        assert [p.relative_to(first) for p in left] == [
            p.relative_to(second) for p in right
        ]
        for a, b in zip(left, right):
            assert a.read_bytes() == b.read_bytes()

    def test_explored_ground_stays_clear_all_the_way_up_the_pyramid(
        self, seeded, tmp_path
    ):
        # 2x2 max is what keeps a thin trail alive at low zoom. If it were a
        # mean, the track would fade out within two levels.
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all")

        for zoom in (14, 12, 10, 8):
            cleared = False
            for tile in root.glob(f"dark/all/fog/{zoom}/*/*.png"):
                if (np.array(Image.open(tile))[..., 3] == 0).any():
                    cleared = True
                    break
            assert cleared, f"fog closed over entirely by zoom {zoom}"

    def test_placeholders_are_written_for_every_theme_and_kind(self, tmp_path):
        written = composite.write_placeholders(tmp_path)
        assert len(written) == 4
        for path in written:
            assert path.is_file()

class TestPerYearViews:
    """Phase 3: every year present in the data gets its own rendered pyramid."""

    @pytest.fixture
    def multi_year(self, conn):
        from datetime import datetime, timezone

        for index, year in enumerate((2022, 2023, 2024)):
            points = [
                (synthetic.BASE_LON + step * 0.0001, synthetic.BASE_LAT + index * 0.0009)
                for step in range(40)
            ]
            document = synthetic.gpx_document(
                points,
                name=f"route {year}",
                start=datetime(year, 5, 1, 8, 0, tzinfo=timezone.utc),
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))
        return conn

    def test_a_view_exists_for_every_year_in_the_data(self, multi_year):
        assert composite.available_views(multi_year) == [
            "all",
            "year:2022",
            "year:2023",
            "year:2024",
        ]

    def test_each_year_renders_its_own_pyramid(self, multi_year, tmp_path):
        root = tmp_path / "tiles"
        rendered = composite.render_all(multi_year, root)

        for year in (2022, 2023, 2024):
            assert rendered[f"year:{year}"] > 0
            assert (root / "dark" / f"year-{year}" / "fog" / "0" / "0" / "0.png").is_file()

    def test_the_years_render_to_different_pixels(self, multi_year, tmp_path):
        root = tmp_path / "tiles"
        composite.render_all(multi_year, root)

        seen = {
            (root / "dark" / f"year-{year}" / "fog" / "13" / "4107" / "4090.png").read_bytes()
            for year in (2022, 2023, 2024)
        }
        assert len(seen) == 3

    def test_the_cumulative_view_is_the_union_of_the_years(self, multi_year, tmp_path):
        root = tmp_path / "tiles"
        composite.render_all(multi_year, root)

        def explored(view):
            tile = root / "dark" / view / "fog" / "13" / "4107" / "4090.png"
            return np.array(Image.open(tile))[..., 3] == 0

        union = np.zeros((TILE, TILE), dtype=bool)
        for year in (2022, 2023, 2024):
            union |= explored(f"year-{year}")

        assert np.array_equal(explored("all"), union)
        assert union.sum() > 0

    def test_undated_tracks_render_under_prehistory(self, conn, tmp_path):
        points = [(synthetic.BASE_LON + n * 0.0001, synthetic.BASE_LAT) for n in range(30)]
        document = synthetic.gpx_document(points, with_time=False)
        common.ingest_tracks(conn, "workout", gpx.parse(document))

        assert "prehistory" in composite.available_views(conn)
        rendered = composite.render_all(conn, tmp_path / "tiles")
        assert rendered["prehistory"] > 0

    def test_render_all_covers_every_available_view(self, seeded, tmp_path):
        rendered = composite.render_all(seeded, tmp_path / "tiles")
        assert set(rendered) == set(composite.available_views(seeded))
        assert all(count > 0 for count in rendered.values())
