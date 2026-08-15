# SPDX-License-Identifier: AGPL-3.0-or-later
"""Colourising, PNG encoding and the tile pyramid on disk."""

from __future__ import annotations

import io
import json

import numpy as np
import pytest
from PIL import Image

from irfaran import composite, db, geo
from irfaran.ingest import common, gpx

from . import synthetic

TILE = geo.TILE_PX


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "irfaran.db")
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
        assert (rgba[..., 3] == composite.fog_alpha()).all()

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
        assert (decoded[..., 3] == composite.fog_alpha()).all()

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

class TestDeepPyramid:
    """Below z14 the pyramid is stamped from geometry, not upscaled.

    A 15 m brush is a two-pixel disc on the native grid. Magnifying that to
    z18 is what made a hand-drawn stroke arrive as a smear, so z15 and z16 are
    rasterised from the same events at their own resolution.
    """

    def test_the_pyramid_reaches_the_deep_levels(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all")

        for zoom in range(15, geo.MAX_Z + 1):
            assert list(root.glob(f"dark/all/fog/{zoom}/*/*.png")), zoom
            assert list(root.glob(f"dark/all/trail/{zoom}/*/*.png")), zoom
        assert not list(root.glob(f"dark/all/fog/{geo.MAX_Z + 1}/*/*.png"))

    def test_a_deep_level_holds_more_detail_than_the_native_one(
        self, seeded, tmp_path
    ):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all")

        # Same ground, four times the pixels across: the explored area should
        # come out close to sixteen times as many pixels, not one to one.
        native = _explored_pixels(root, geo.NATIVE_Z)
        deep = _explored_pixels(root, geo.MAX_Z)
        factor = 4 ** (geo.MAX_Z - geo.NATIVE_Z)

        assert native > 0
        assert deep > native * factor * 0.5

    def test_an_erase_still_applies_at_the_deep_levels(self, conn, tmp_path):
        from irfaran import raster

        root = tmp_path / "tiles"
        line = synthetic.straight_line(40)
        _insert(conn, line, radius_m=20.0)
        composite.render_view(conn, root, "all")
        before = _explored_pixels(root, geo.MAX_Z)

        _insert(conn, line[10:25], op="erase", layers=["*"], radius_m=20.0)
        composite.render_view(conn, root, "all")

        assert _explored_pixels(root, geo.MAX_Z) < before
        assert raster is not None

    def test_redrawing_over_an_erase_works_at_the_deep_levels(self, conn, tmp_path):
        root = tmp_path / "tiles"
        line = synthetic.straight_line(40)
        _insert(conn, line, radius_m=20.0)
        _insert(conn, line[10:25], op="erase", layers=["*"], radius_m=20.0)
        composite.render_view(conn, root, "all")
        erased = _explored_pixels(root, geo.MAX_Z)

        _insert(conn, line[12:22], source="manual", radius_m=20.0)
        composite.render_view(conn, root, "all")

        assert _explored_pixels(root, geo.MAX_Z) > erased

    def test_the_deep_levels_are_reproducible(self, seeded, tmp_path):
        first, second = tmp_path / "a", tmp_path / "b"
        composite.render_view(seeded, first, "all")
        composite.render_view(seeded, second, "all")

        left = sorted(first.rglob(f"dark/all/fog/{geo.MAX_Z}/*/*.png"))
        right = sorted(second.rglob(f"dark/all/fog/{geo.MAX_Z}/*/*.png"))
        assert left and len(left) == len(right)
        for a, b in zip(left, right):
            assert a.read_bytes() == b.read_bytes()

    def test_a_year_view_only_holds_its_own_year_at_depth(self, conn, tmp_path):
        from datetime import datetime, timezone

        root = tmp_path / "tiles"
        for year in (2023, 2024):
            document = synthetic.gpx_document(
                synthetic.straight_line(40),
                name=f"route {year}",
                start=datetime(year, 5, 1, 8, 0, tzinfo=timezone.utc),
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))

        composite.render_all(conn, root)

        one = _explored_pixels(root, geo.MAX_Z, view="year-2024")
        both = _explored_pixels(root, geo.MAX_Z, view="all")
        assert 0 < one <= both

    def test_the_scope_of_an_edit_includes_its_deep_tiles(self):
        scope = composite.rebuild_scope({(8937, 5681)})
        assert sorted(scope) == list(range(0, geo.MAX_Z + 1))
        assert len(scope[15]) == 4
        assert len(scope[16]) == 16


def _insert(conn, points, *, op="add", layers=("2024",), source="workout", radius_m=15.0):
    """Insert an event and rasterise it onto the native grid."""
    from irfaran import raster

    cursor = conn.execute(
        "INSERT INTO events "
        "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, NULL, '2024-05-01T08:00:00+00:00', NULL)",
        (
            source,
            op,
            json.dumps(
                {"type": "LineString", "coordinates": [[x, y] for x, y in points]}
            ),
            radius_m,
            json.dumps(list(layers)),
        ),
    )
    row = conn.execute(
        "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return raster.stamp_event(conn, row)


def _explored_pixels(root, zoom: int, view: str = "all") -> int:
    total = 0
    for tile in root.glob(f"dark/{view}/fog/{zoom}/*/*.png"):
        total += int((np.array(Image.open(tile))[..., 3] == 0).sum())
    return total


class TestParallelRender:
    """More cores must only make it faster, never different.

    Rendering fans out over (view, native tile) jobs in separate processes.
    Every one of them writes PNGs to the same tree, so the thing worth testing
    is not the speed - it is that the tree is byte for byte what one process
    would have produced.
    """

    @pytest.fixture
    def multi_year(self, conn):
        from datetime import datetime, timezone

        for index, year in enumerate((2022, 2023, 2024)):
            points = [
                (synthetic.BASE_LON + index * 0.06 + step * 0.0002, synthetic.BASE_LAT)
                for step in range(40)
            ]
            document = synthetic.gpx_document(
                points,
                name=f"route {year}",
                start=datetime(year, 5, 1, 8, 0, tzinfo=timezone.utc),
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))
        return conn

    def test_parallel_and_serial_agree_byte_for_byte(self, multi_year, tmp_path):
        serial, parallel = tmp_path / "serial", tmp_path / "parallel"
        composite.render_views(
            multi_year, serial, composite.available_views(multi_year), workers=1
        )
        composite.render_views(
            multi_year, parallel, composite.available_views(multi_year), workers=4
        )

        left = sorted(serial.rglob("*.png"))
        right = sorted(parallel.rglob("*.png"))
        assert left, "nothing was rendered"
        assert [p.relative_to(serial) for p in left] == [
            p.relative_to(parallel) for p in right
        ]
        for a, b in zip(left, right):
            assert a.read_bytes() == b.read_bytes(), a.relative_to(serial)

    def test_the_counts_agree_too(self, multi_year, tmp_path):
        views = composite.available_views(multi_year)
        one = composite.render_views(multi_year, tmp_path / "a", views, workers=1)
        many = composite.render_views(multi_year, tmp_path / "b", views, workers=4)
        assert one == many
        assert sum(one.values()) > 0

    def test_a_view_that_lost_its_data_is_still_pruned(self, conn, tmp_path):
        root = tmp_path / "tiles"
        document = synthetic.gpx_document(synthetic.square_loop(40))
        common.ingest_tracks(conn, "workout", gpx.parse(document))
        composite.render_views(conn, root, ["all"], workers=4)
        assert list(root.glob("dark/all/fog/14/*/*.png"))

        conn.execute("DELETE FROM blobs")
        composite.render_views(conn, root, ["all"], workers=4)
        assert not list(root.glob("dark/all/fog/14/*/*.png"))

    def test_workers_are_configurable_and_sane(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_RENDER_WORKERS", "3")
        assert composite.render_workers() == 3

        monkeypatch.setenv("IRFARAN_RENDER_WORKERS", "0")
        assert composite.render_workers() == 1

        monkeypatch.setenv("IRFARAN_RENDER_WORKERS", "loads")
        with pytest.raises(ValueError, match="whole number"):
            composite.render_workers()

        monkeypatch.delenv("IRFARAN_RENDER_WORKERS")
        assert composite.render_workers() >= 1


class TestFogColour:
    """The colour of the unknown is a stored setting, baked into the tiles."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("#1c1e23", (28, 30, 35)),
            ("1c1e23", (28, 30, 35)),
            ("  #FFF  ", (255, 255, 255)),
            ("#0a0b0c", (10, 11, 12)),
        ],
    )
    def test_hex_reads_back_as_a_colour(self, raw, expected):
        assert composite.parse_colour(raw, "test") == expected

    @pytest.mark.parametrize("bad", ["", "#12345", "orange", "#gg0011", "#12345678"])
    def test_anything_else_is_refused_by_name(self, bad):
        with pytest.raises(ValueError, match="hex colour"):
            composite.parse_colour(bad, "IRFARAN_FOG_COLOUR_DARK")

    def test_the_built_in_is_used_when_nothing_is_set(self, conn):
        assert composite.fog_colour("dark", conn) == composite.FOG_COLOUR["dark"]

    def test_a_stored_setting_wins_over_the_built_in(self, conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('fog_colour_dark', '#402030') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        assert composite.fog_colour("dark", conn) == (64, 32, 48)
        assert composite.fog_colour("light", conn) == composite.FOG_COLOUR["light"]

    def test_the_environment_wins_over_the_setting(self, conn, monkeypatch):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('fog_colour_dark', '#402030') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        monkeypatch.setenv("IRFARAN_FOG_COLOUR_DARK", "#010203")
        assert composite.fog_colour("dark", conn) == (1, 2, 3)

    def test_an_unknown_theme_is_refused_by_name(self, conn):
        with pytest.raises(ValueError, match="Unknown theme 'sepia'"):
            composite.fog_colour("sepia", conn)

    def test_the_setting_reaches_the_rendered_tiles(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all", themes=("dark",))
        before = _fog_rgb(root)

        seeded.execute(
            "INSERT INTO settings (key, value) VALUES ('fog_colour_dark', '#402030') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        composite.render_view(seeded, root, "all", themes=("dark",))

        assert before != (64, 32, 48)
        assert _fog_rgb(root) == (64, 32, 48)

    def test_the_placeholder_follows_the_setting(self, conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('fog_colour_dark', '#402030') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        # Most of the world is a tile with no data, so the placeholder has to
        # be recoloured too or the map goes two-tone.
        empty = composite.placeholder_tile("dark", "fog", conn)
        pixels = np.array(Image.open(io.BytesIO(empty)))
        assert tuple(pixels[0, 0, :3]) == (64, 32, 48)


def _fog_rgb(root) -> tuple[int, int, int]:
    tile = root / "dark" / "all" / "fog" / "0" / "0" / "0.png"
    return tuple(int(v) for v in np.array(Image.open(tile))[0, 0, :3])


class TestScopedRender:
    """Section 6: an edit rebuilds its tiles and their ancestors, not a view.

    Every one of these compares a scoped render against the full render of the
    same database. Scoping is only ever allowed to make rendering cheaper - if
    it can also make it different, undo and import quietly stop agreeing with
    a rebuild, which is invariant 1.
    """

    def test_a_scoped_render_writes_only_the_scope(self, conn, tmp_path):
        root = tmp_path / "tiles"
        from datetime import datetime, timezone

        # Two tracks far enough apart to land in different z14 tiles, so the
        # whole-view render has strictly more to do than one tile's chain. The
        # start times differ or the second import is deduplicated away.
        for index, offset in enumerate((0.0, 0.08)):
            points = [
                (synthetic.BASE_LON + offset + n * 0.0001, synthetic.BASE_LAT)
                for n in range(40)
            ]
            document = synthetic.gpx_document(
                points,
                name=f"track {index}",
                start=datetime(2024, 5, 1, 8 + index, 0, tzinfo=timezone.utc),
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))

        native = composite.tiles_with_data(conn, None)
        assert len(native) > 1

        full = composite.render_view(conn, root, "all")
        scoped = composite.render_view(
            conn, root, "all", scope=composite.rebuild_scope({sorted(native)[0]})
        )

        # Fifteen zooms up to z14, two themes, two kinds - plus however many
        # of the twenty deep tiles under that one native tile hold anything.
        chain = (geo.NATIVE_Z + 1) * 2 * 2
        deep_ceiling = sum(4 ** (z - geo.NATIVE_Z) for z in range(15, geo.MAX_Z + 1))
        assert chain <= scoped <= chain + deep_ceiling * 2 * 2
        assert scoped < full

    def test_a_scoped_render_leaves_the_tiles_outside_it_alone(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all")
        before = {path: path.read_bytes() for path in sorted(root.rglob("*.png"))}

        touched = {sorted(composite.tiles_with_data(seeded, None))[0]}
        composite.render_view(
            seeded, root, "all", scope=composite.rebuild_scope(touched)
        )

        after = {path: path.read_bytes() for path in sorted(root.rglob("*.png"))}
        assert after == before

    def test_a_scoped_render_still_removes_tiles_that_lost_their_data(
        self, conn, tmp_path
    ):
        from irfaran import raster

        root = tmp_path / "tiles"
        document = synthetic.gpx_document(synthetic.square_loop(40))
        result = common.ingest_tracks(conn, "workout", gpx.parse(document))
        composite.render_view(conn, root, "all")
        assert list(root.glob("dark/all/fog/14/*/*.png"))

        conn.execute("DELETE FROM events")
        raster.rebuild_tiles(conn, result.tiles_touched)
        composite.render_view(
            conn, root, "all", scope=composite.rebuild_scope(result.tiles_touched)
        )

        assert not list(root.glob("dark/all/fog/14/*/*.png"))

    def test_a_scoped_render_matches_a_full_one_after_a_new_event(
        self, seeded, tmp_path
    ):
        from irfaran import raster

        scoped_root = tmp_path / "scoped"
        full_root = tmp_path / "full"
        composite.render_view(seeded, scoped_root, "all")

        points = [
            (synthetic.BASE_LON + n * 0.0001, synthetic.BASE_LAT + 0.0004)
            for n in range(30)
        ]
        cursor = seeded.execute(
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES ('manual', 'add', ?, 15.0, '[\"2024\"]', NULL, "
            "'2024-05-01T08:00:00+00:00', NULL)",
            (json.dumps({"type": "LineString", "coordinates": points}),),
        )
        row = seeded.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        touched = raster.stamp_event(seeded, row)

        composite.render_view(
            seeded, scoped_root, "all", scope=composite.rebuild_scope(touched)
        )
        composite.render_view(seeded, full_root, "all")

        scoped = sorted(scoped_root.rglob("*.png"))
        full = sorted(full_root.rglob("*.png"))
        assert [p.relative_to(scoped_root) for p in scoped] == [
            p.relative_to(full_root) for p in full
        ]
        for a, b in zip(scoped, full):
            assert a.read_bytes() == b.read_bytes(), a.relative_to(scoped_root)


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


class TestTrailColours:
    """The ramp is a stored setting, baked into the tiles like the fog colour."""

    def test_the_built_in_is_ember(self, conn):
        assert composite.trail_ramp(conn) == "ember"

    def test_a_stored_setting_wins(self, conn):
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('trail_ramp', 'ice') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        assert composite.trail_ramp(conn) == "ice"

    def test_an_unknown_ramp_is_refused_by_name(self, conn):
        with pytest.raises(ValueError, match="Unknown trail colours 'tartan'"):
            composite.check_ramp("tartan")

    @pytest.mark.parametrize("ramp", sorted(composite.TRAIL_RAMP_SETS))
    @pytest.mark.parametrize("theme", composite.THEMES)
    def test_every_ramp_has_both_themes_and_a_sane_table(self, ramp, theme):
        lut = composite.trail_lut(theme, ramp)
        assert lut.shape == (256, 4)
        # Never crossed is invisible; crossed once is not.
        assert lut[0][3] == 0
        assert lut[1][3] > 0
        # More passes is never less visible.
        assert lut[255][3] >= lut[1][3]

    def test_the_ramps_differ_from_each_other(self, conn):
        seen = {
            ramp: composite.trail_lut("dark", ramp)[200].tobytes()
            for ramp in composite.TRAIL_RAMP_SETS
        }
        assert len(set(seen.values())) == len(seen)

    def test_the_setting_reaches_the_rendered_tiles(self, seeded, tmp_path):
        root = tmp_path / "tiles"
        composite.render_view(seeded, root, "all", themes=("dark",))
        ember = _trail_rgb(root)

        seeded.execute(
            "INSERT INTO settings (key, value) VALUES ('trail_ramp', 'ice') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )
        composite.render_view(seeded, root, "all", themes=("dark",))

        assert ember is not None and _trail_rgb(root) != ember


class TestDeepTrailSoftening:
    """Below the native grid a track is wide enough that hard edges show."""

    def test_softening_only_ever_adds_glow(self):
        trail = np.zeros((composite.TILE, composite.TILE), dtype=np.uint8)
        trail[100:104, 60:200] = 40

        hard = composite.render_trail(trail, "dark")
        soft = composite.render_trail(trail, "dark", "ember", composite.DEEP_TRAIL_SOFT_PX)

        # The line itself is no dimmer, and its surroundings are brighter.
        assert (soft[..., 3].astype(int) >= hard[..., 3].astype(int)).all()
        assert soft[..., 3].sum() > hard[..., 3].sum()

    def test_an_empty_tile_is_untouched(self):
        trail = np.zeros((composite.TILE, composite.TILE), dtype=np.uint8)
        hard = composite.render_trail(trail, "dark")
        soft = composite.render_trail(trail, "dark", "ember", 2.0)
        assert np.array_equal(hard, soft)


def _trail_rgb(root):
    tile = root / "dark" / "all" / "trail" / "14"
    for path in sorted(tile.rglob("*.png")):
        pixels = np.array(Image.open(path))
        lit = pixels[..., 3] > 0
        if lit.any():
            return tuple(int(v) for v in pixels[lit][:, :3].mean(axis=0).round())
    return None
