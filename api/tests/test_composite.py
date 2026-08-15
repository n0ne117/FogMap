# SPDX-License-Identifier: AGPL-3.0-or-later
"""Composite semantics, section 6.

These four are the required tests, and they exist because erase is the thing
most likely to be quietly broken by a refactor. Erase is a subtract mask
applied when a view is composed - never a bit cleared in the fog blob - so it
has to survive a rebuild and a re-import of the file that drew the fog under it.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from fogmap import composite, db, geo, raster
from fogmap.ingest import common, gpx

from . import synthetic

TILE = geo.TILE_PX


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "fogmap.db")
    yield connection
    connection.close()


def add_event(
    conn,
    points,
    *,
    op="add",
    layers=("2024",),
    source="workout",
    radius_m=15.0,
    external_id=None,
):
    """Insert an event directly and rasterise it."""
    geometry = json.dumps(
        {"type": "LineString", "coordinates": [[lon, lat] for lon, lat in points]}
    )
    cursor = conn.execute(
        "INSERT INTO events "
        "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
        (
            source,
            op,
            geometry,
            radius_m,
            json.dumps(list(layers)),
            external_id,
            "2024-03-01T09:00:00+00:00",
        ),
    )
    event_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    touched = raster.stamp_event(conn, row)
    return event_id, touched


def fog_of(conn, view="all"):
    """Composite the tile the synthetic fixtures live on."""
    tile_x, tile_y = geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
    fog, _ = composite.composite_tile(conn, view, tile_x, tile_y)
    return fog


def trail_of(conn, view="all"):
    tile_x, tile_y = geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
    _, trail = composite.composite_tile(conn, view, tile_x, tile_y)
    return trail


class TestEraseSurvivesRebuild:
    """Required test 1."""

    def test_erase_still_applies_after_a_full_rebuild(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        before_erase = fog_of(conn).sum()

        add_event(conn, line[10:20], op="erase", layers=[raster.ERASE_LAYER])
        after_erase = fog_of(conn)
        assert after_erase.sum() < before_erase

        raster.rebuild(conn)

        assert np.array_equal(fog_of(conn), after_erase)
        assert fog_of(conn).sum() < before_erase

    def test_rebuild_reproduces_the_blob_store_byte_for_byte(self, conn):
        add_event(conn, synthetic.square_loop())
        add_event(conn, synthetic.straight_line(20), op="erase", layers=["*"])

        before = _blob_snapshot(conn)
        raster.rebuild(conn)
        assert _blob_snapshot(conn) == before


class TestEraseSurvivesReimport:
    """Required test 2."""

    def test_reimporting_the_source_file_does_not_resurrect_erased_fog(self, conn):
        document = synthetic.gpx_document(synthetic.straight_line(40))
        tracks = gpx.parse(document, filename="synthetic.gpx")

        common.ingest_tracks(conn, "workout", tracks)
        with_fog = fog_of(conn).sum()

        add_event(conn, synthetic.straight_line(40)[10:20], op="erase", layers=["*"])
        erased = fog_of(conn)
        assert erased.sum() < with_fog

        # The identical file again. Dedup means no new events, and the erase
        # is untouched either way because it is applied at composite time.
        again = common.ingest_tracks(conn, "workout", gpx.parse(document))
        assert again.events_created == 0
        assert again.events_skipped >= 1

        assert np.array_equal(fog_of(conn), erased)


class TestDrawingOverErasedGround:
    """The eraser must not be a one-way door.

    Erase is a composite-time subtract, so a later stroke over erased ground
    used to vanish: it went into the fog blob and was subtracted straight back
    out. Invariant 2 covers rebuilds and re-imports, not deliberate redrawing.
    """

    def test_a_later_stroke_reappears_where_it_was_erased(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        add_event(conn, line[10:20], op="erase", layers=[raster.ERASE_LAYER])
        erased = fog_of(conn)

        add_event(conn, line[12:18], source="manual")

        redrawn = fog_of(conn)
        assert redrawn.sum() > erased.sum()

    def test_redrawing_only_lifts_the_erase_it_covered(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        before_erase = fog_of(conn)
        add_event(conn, line[10:30], op="erase", layers=[raster.ERASE_LAYER])

        add_event(conn, line[12:18], source="manual")
        redrawn = fog_of(conn)

        # The rest of the erase is still doing its job.
        assert redrawn.sum() < before_erase.sum()

    def test_the_erase_still_wins_when_it_came_last(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        add_event(conn, line[12:18], source="manual")
        add_event(conn, line[10:20], op="erase", layers=[raster.ERASE_LAYER])

        assert fog_of(conn).sum() < fog_of(conn).size

    def test_redrawing_survives_a_rebuild_byte_for_byte(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        add_event(conn, line[10:20], op="erase", layers=[raster.ERASE_LAYER])
        add_event(conn, line[12:18], source="manual")

        before = _blob_snapshot(conn)
        raster.rebuild(conn)
        assert _blob_snapshot(conn) == before


class TestDeletingAnEraseRestoresFog:
    """Required test 3."""

    def test_removing_the_erase_event_brings_the_fog_back(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        original = fog_of(conn).copy()

        erase_id, _ = add_event(conn, line[10:20], op="erase", layers=["*"])
        assert fog_of(conn).sum() < original.sum()

        conn.execute("DELETE FROM events WHERE id = ?", (erase_id,))
        raster.rebuild(conn)

        assert np.array_equal(fog_of(conn), original)

    def test_deleting_the_erase_also_restores_the_trail_underneath(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        original = trail_of(conn).copy()

        erase_id, _ = add_event(conn, line[10:20], op="erase", layers=["*"])
        assert trail_of(conn).sum() < original.sum()

        conn.execute("DELETE FROM events WHERE id = ?", (erase_id,))
        raster.rebuild(conn)
        assert np.array_equal(trail_of(conn), original)


class TestAllViewEqualsUnionOfYears:
    """Required test 4."""

    def test_all_is_the_or_of_every_per_year_view(self, conn):
        add_event(conn, synthetic.straight_line(30), layers=["2023"])
        add_event(
            conn,
            [(lon, lat + 0.002) for lon, lat in synthetic.straight_line(30)],
            layers=["2024"],
        )

        union = fog_of(conn, "year:2023") | fog_of(conn, "year:2024")
        assert np.array_equal(fog_of(conn, "all"), union)
        assert union.sum() > 0

    def test_the_union_still_holds_once_an_erase_is_in_play(self, conn):
        line = synthetic.straight_line(30)
        add_event(conn, line, layers=["2023"])
        add_event(conn, [(lon, lat + 0.002) for lon, lat in line], layers=["2024"])
        add_event(conn, line[5:15], op="erase", layers=["*"])

        union = fog_of(conn, "year:2023") | fog_of(conn, "year:2024")
        assert np.array_equal(fog_of(conn, "all"), union)

    def test_a_year_view_excludes_the_other_year(self, conn):
        add_event(conn, synthetic.straight_line(30), layers=["2023"])
        assert fog_of(conn, "year:2023").sum() > 0
        assert fog_of(conn, "year:2024").sum() == 0


class TestEraseIgnoresTheLayerFilter:
    def test_an_erase_drawn_once_applies_to_every_year(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line, layers=["2023"])
        add_event(conn, line, layers=["2024"])

        add_event(conn, line[10:20], op="erase", layers=["*"], source="manual")

        # Neither year keeps the erased stretch, even though the erase was
        # never tagged with either of them.
        assert fog_of(conn, "year:2023").sum() < fog_of(conn, "year:2023").size
        for view in ("all", "year:2023", "year:2024"):
            fog = fog_of(conn, view)
            erased = composite.erase_mask(
                conn, *geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
            )
            assert not (fog & erased).any()

    def test_trail_is_masked_by_erase_too(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line)
        add_event(conn, line[10:20], op="erase", layers=["*"])

        tile = geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
        erased = composite.erase_mask(conn, *tile)
        assert (trail_of(conn)[erased] == 0).all()


class TestTrailAccumulation:
    def test_a_second_pass_raises_the_count(self, conn):
        line = synthetic.straight_line(30)
        add_event(conn, line, external_id="pass-1")
        first = trail_of(conn).max()

        add_event(conn, line, external_id="pass-2")
        assert trail_of(conn).max() == first + 1

    def test_the_count_saturates_rather_than_wrapping(self, conn):
        tiles = {(0, 0): np.ones((TILE, TILE), dtype=bool)}
        raster.write_blob(
            conn, "trail", "workout", "2024", 0, 0,
            np.full((TILE, TILE), 255, dtype=np.uint8),
        )
        raster.merge_trail(conn, "workout", "2024", tiles)

        stored = raster.read_blob(conn, "trail", "workout", "2024", 0, 0)
        assert stored.max() == 255


class TestDownsampling:
    def test_fog_uses_a_two_by_two_max_so_thin_trails_survive(self):
        tile = np.zeros((TILE, TILE), dtype=np.uint8)
        tile[0, 0] = 255
        assert composite.downsample_max(tile)[0, 0] == 255

    def test_trail_uses_a_two_by_two_sum(self):
        tile = np.full((TILE, TILE), 2, dtype=np.uint8)
        assert composite.downsample_sum(tile)[0, 0] == 8

    def test_the_trail_sum_saturates_at_255(self):
        tile = np.full((TILE, TILE), 200, dtype=np.uint8)
        assert composite.downsample_sum(tile).max() == 255

    def test_a_parent_is_assembled_from_four_quadrants(self):
        children = {
            (0, 0): np.full((TILE, TILE), 1, dtype=np.uint8),
            (1, 1): np.full((TILE, TILE), 2, dtype=np.uint8),
        }
        parent = composite.parent_tile(children, "sum")

        assert parent[0, 0] == 4
        assert parent[TILE - 1, TILE - 1] == 8
        assert parent[0, TILE - 1] == 0  # north-east quadrant was never supplied

    def test_an_unknown_downsampling_mode_is_rejected(self):
        with pytest.raises(ValueError, match="'max' for fog and erase"):
            composite.parent_tile({}, "mean")


class TestRebuildScope:
    def test_a_touched_tile_pulls_in_all_fourteen_ancestors(self):
        scope = composite.rebuild_scope({(8937, 5681)})
        assert sorted(scope) == list(range(0, geo.MAX_Z + 1))
        assert scope[14] == {(8937, 5681)}
        assert scope[0] == {(0, 0)}

    def test_neighbouring_tiles_share_ancestors_rather_than_duplicating_them(self):
        scope = composite.rebuild_scope({(8936, 5680), (8937, 5681)})
        assert len(scope[14]) == 2
        assert scope[0] == {(0, 0)}

    def test_nothing_touched_means_nothing_to_rebuild(self):
        assert composite.rebuild_scope(set()) == {14: set()}


class TestViewsTouching:
    """An erase re-renders every view that has anything where it landed."""

    def test_only_the_views_with_pixels_in_the_tiles_come_back(self, conn):
        add_event(conn, synthetic.straight_line(20), layers=["2024"])
        far = [(lon + 0.5, lat) for lon, lat in synthetic.straight_line(20)]
        add_event(conn, far, layers=["2015"])

        near_tiles = composite.tiles_with_data(conn, {"2024"})
        assert composite.views_touching(conn, near_tiles) == ["all", "year:2024"]

        everything = composite.tiles_with_data(conn, None)
        assert composite.views_touching(conn, everything) == [
            "all",
            "year:2015",
            "year:2024",
        ]

    def test_nowhere_touches_nothing(self, conn):
        add_event(conn, synthetic.straight_line(20), layers=["2024"])
        assert composite.views_touching(conn, set()) == []

    def test_empty_ground_still_gets_the_cumulative_view(self, conn):
        add_event(conn, synthetic.straight_line(20), layers=["2024"])
        assert composite.views_touching(conn, {(1, 1)}) == ["all"]


class TestViewNames:
    def test_the_canonical_views_resolve(self):
        assert composite.view_layers("all") is None
        assert composite.view_layers("prehistory") == {"prehistory"}
        assert composite.view_layers("year:1994") == {"1994"}

    @pytest.mark.parametrize("bad", ["year:94", "year:", "2024", "everything"])
    def test_anything_else_is_refused_by_name(self, bad):
        with pytest.raises(ValueError, match=bad.split(":")[0][:4]):
            composite.view_layers(bad)

    def test_available_views_lists_only_what_has_pixels(self, conn):
        assert composite.available_views(conn) == ["all"]

        add_event(conn, synthetic.straight_line(20), layers=["2024"])
        add_event(conn, synthetic.straight_line(20), layers=["prehistory"])

        assert composite.available_views(conn) == ["all", "year:2024", "prehistory"]


def _blob_snapshot(conn) -> dict[tuple, bytes]:
    return {
        (row["kind"], row["source"], row["layer"], row["x"], row["y"]): bytes(
            row["data"]
        )
        for row in conn.execute("SELECT * FROM blobs")
    }


class TestRevealLeavesNoTrack:
    """A reveal clears fog and nothing else.

    Somebody was around here is a different claim from somebody walked along
    here, and a childhood village or a week on an island is the first without
    being the second. Drawing a route through it would be inventing one.
    """

    def test_a_reveal_clears_fog(self, conn):
        line = synthetic.straight_line(30)
        add_event(conn, line, op="reveal", source="manual")
        assert fog_of(conn).sum() > 0

    def test_a_reveal_records_no_pass_on_the_trail(self, conn):
        line = synthetic.straight_line(30)
        add_event(conn, line, op="reveal", source="manual")

        trails = conn.execute(
            "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'trail'"
        ).fetchone()["n"]
        assert trails == 0

    def test_an_add_over_the_same_ground_does_record_one(self, conn):
        line = synthetic.straight_line(30)
        add_event(conn, line, op="add", source="manual")

        trails = conn.execute(
            "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'trail'"
        ).fetchone()["n"]
        assert trails > 0

    def test_a_reveal_survives_a_rebuild_byte_for_byte(self, conn):
        add_event(conn, synthetic.straight_line(30), op="reveal", source="manual")
        add_event(conn, synthetic.square_loop(20), op="add")

        before = _blob_snapshot(conn)
        raster.rebuild(conn)
        assert _blob_snapshot(conn) == before

    def test_a_reveal_can_be_erased_and_redrawn_like_anything_else(self, conn):
        line = synthetic.straight_line(40)
        add_event(conn, line, op="reveal", source="manual")
        revealed = fog_of(conn).sum()

        add_event(conn, line[10:25], op="erase", layers=[raster.ERASE_LAYER])
        assert fog_of(conn).sum() < revealed

        add_event(conn, line[12:22], op="reveal", source="manual")
        assert fog_of(conn).sum() > 0


class TestAreaReveal:
    """An area is a Polygon, and filling it is the point."""

    def square(self, side: float = 0.004) -> list[tuple[float, float]]:
        lon, lat = synthetic.BASE_LON, synthetic.BASE_LAT
        return [
            (lon, lat),
            (lon + side, lat),
            (lon + side, lat + side),
            (lon, lat + side),
            (lon, lat),
        ]

    def polygon(self, ring) -> str:
        return json.dumps(
            {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in ring]]}
        )

    def insert(self, conn, ring, op="reveal", layers=("2024",), radius_m=15.0):
        cursor = conn.execute(
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES ('manual', ?, ?, ?, ?, NULL, '2024-05-01T08:00:00+00:00', NULL)",
            (op, self.polygon(ring), radius_m, json.dumps(list(layers))),
        )
        row = conn.execute(
            "SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return raster.stamp_event(conn, row)

    def test_the_inside_is_cleared_not_just_the_edge(self, conn):
        self.insert(conn, self.square())
        fog = fog_of(conn)

        # A ring stamped as a line would leave the middle under fog. Compare
        # the filled area against the perimeter it would have covered.
        assert fog.sum() > 0
        rows, cols = np.nonzero(fog)
        height = rows.max() - rows.min() + 1
        width = cols.max() - cols.min() + 1
        assert fog.sum() > 0.8 * height * width

    def test_an_area_leaves_no_track(self, conn):
        self.insert(conn, self.square())
        trails = conn.execute(
            "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'trail'"
        ).fetchone()["n"]
        assert trails == 0

    def test_an_unclosed_ring_is_closed_for_you(self, conn):
        lon, lat = synthetic.BASE_LON, synthetic.BASE_LAT
        side = 0.004
        open_ring = [(lon, lat), (lon + side, lat), (lon + side, lat + side)]
        self.insert(conn, open_ring)
        assert fog_of(conn).sum() > 0

    def test_a_ring_with_too_few_corners_is_refused_by_name(self, conn):
        with pytest.raises(ValueError, match="needs at least three"):
            self.insert(conn, [(synthetic.BASE_LON, synthetic.BASE_LAT)] * 2)

    def test_an_area_survives_a_rebuild_byte_for_byte(self, conn):
        self.insert(conn, self.square())
        before = _blob_snapshot(conn)
        raster.rebuild(conn)
        assert _blob_snapshot(conn) == before

    def test_the_deep_levels_fill_it_too(self, conn, tmp_path):
        from fogmap import geo
        from PIL import Image

        self.insert(conn, self.square())
        root = tmp_path / "tiles"
        composite.render_view(conn, root, "all")

        cleared = 0
        for tile in root.glob(f"dark/all/fog/{geo.MAX_Z}/*/*.png"):
            cleared += int((np.array(Image.open(tile))[..., 3] == 0).sum())
        assert cleared > 0
