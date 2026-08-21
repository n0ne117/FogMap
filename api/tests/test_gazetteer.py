# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading names out of the basemap, and searching them.

Three pieces, each tested where it can be tested without a 137 GB file: the
PMTiles reader's arithmetic, the vector-tile decoder against a tile built here
byte by byte, and the gazetteer's own generations and lookups against rows put in
by hand.

The decoder is the part worth building a fixture for. It was written rather than
depended on, so nothing else checks it, and it is the piece that would fail
silently - a wrong varint gives a name at the wrong coordinates rather than an
error. On the real archive it finds Wien at 48.2082, 16.3724 and a pizzeria in
Caorle; here it has to find a point that was encoded on purpose.
"""

from __future__ import annotations

import struct

import pytest

from irfaran import db, gazetteer, mvt, pmtiles, search

TOKEN = "gazetteer-token"


# -- building a vector tile by hand ----------------------------------------


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _tag(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _length_delimited(field: int, payload: bytes) -> bytes:
    return _tag(field, 2) + _varint(len(payload)) + payload


def _zigzag(value: int) -> int:
    return (value << 1) ^ (value >> 31) if value >= 0 else ((-value) << 1) - 1


def a_tile(layer: str, name: str, x: int, y: int, extent: int = 4096, **extra) -> bytes:
    """One layer, one named point feature, encoded as the specification says."""
    keys = ["name", *extra]
    values = [name, *(str(value) for value in extra.values())]

    body = _length_delimited(1, layer.encode())
    body += _tag(15, 0) + _varint(2)          # version
    body += _tag(5, 0) + _varint(extent)      # extent

    tags: list[int] = []
    for index in range(len(keys)):
        tags.extend((index, index))

    feature = _tag(1, 0) + _varint(1)                                  # id
    feature += _length_delimited(2, b"".join(_varint(t) for t in tags))  # tags
    feature += _tag(3, 0) + _varint(1)                                  # POINT
    geometry = _varint((1 << 3) | 1) + _varint(_zigzag(x)) + _varint(_zigzag(y))
    feature += _length_delimited(4, geometry)
    body += _length_delimited(2, feature)

    for key in keys:
        body += _length_delimited(3, key.encode())
    for value in values:
        body += _length_delimited(4, _length_delimited(1, str(value).encode()))

    return _length_delimited(3, body)


class TestTheVectorTileDecoder:
    def test_it_finds_a_named_point(self) -> None:
        blob = a_tile("places", "Testville", 2048, 1024)
        found = list(mvt.points(blob, {"places"}))
        assert len(found) == 1

        layer, attrs, x, y, extent = found[0]
        assert layer == "places"
        assert attrs["name"] == "Testville"
        assert (x, y, extent) == (2048, 1024, 4096)

    def test_it_reads_the_other_attributes(self) -> None:
        blob = a_tile("pois", "Pizzeria Eleven", 100, 200, kind="restaurant")
        _layer, attrs, *_ = next(iter(mvt.points(blob, {"pois"})))
        assert attrs["kind"] == "restaurant"

    def test_a_layer_nobody_asked_for_is_skipped(self) -> None:
        blob = a_tile("roads", "Some Street", 10, 10)
        assert list(mvt.points(blob, {"places", "pois"})) == []

    def test_the_middle_of_a_tile_is_the_middle_of_its_span(self) -> None:
        """The coordinate maths, which is where a silent error would live."""
        lon, lat = mvt.lonlat(0, 0, 0, 2048, 2048, 4096)
        assert lon == pytest.approx(0.0)
        assert lat == pytest.approx(0.0, abs=1e-9)

    def test_a_tile_at_depth_lands_where_it_should(self) -> None:
        """z10 tile 545,363 covers Vienna, so its middle must be near Vienna."""
        lon, lat = mvt.lonlat(10, 558, 355, 2048, 2048, 4096)
        assert 15.0 < lon < 17.5
        assert 47.0 < lat < 49.0

    def test_rubbish_is_not_mistaken_for_a_tile(self) -> None:
        with pytest.raises(Exception):
            list(mvt.points(b"\xff\xff\xff\xff not a tile", {"places"}))


class TestTheArchiveArithmetic:
    @pytest.mark.parametrize(
        "tile_id, expect",
        [(0, (0, 0, 0)), (1, (1, 0, 0)), (2, (1, 0, 1)), (3, (1, 1, 1)), (4, (1, 1, 0)), (5, (2, 0, 0))],
    )
    def test_hilbert_ids_land_on_the_right_tiles(self, tile_id, expect) -> None:
        assert pmtiles.tile_id_to_zxy(tile_id) == expect

    @pytest.mark.parametrize("zoom", [0, 1, 5, 10, 14])
    def test_the_mapping_round_trips(self, zoom) -> None:
        """Forwards and back, since a resume cursor depends on both.

        Coordinates are clamped to the zoom: at z0 the only tile is 0,0, and
        asking about 1,0 there tests nothing except the test's own arithmetic.
        """
        edge = (1 << zoom) - 1
        corners = {(0, 0), (edge, 0), (0, edge), (edge, edge)}
        for x, y in corners:
            tile_id = pmtiles.zxy_to_tile_id(zoom, x, y)
            assert pmtiles.tile_id_to_zxy(tile_id) == (zoom, x, y)

    def test_a_zoom_is_one_contiguous_range(self) -> None:
        """What makes scanning one level cheap instead of walking everything."""
        first, last = pmtiles.zoom_range(10) if hasattr(pmtiles, "zoom_range") else (0, 0)
        del first, last  # the method lives on Archive; the arithmetic is below
        low = pmtiles.zxy_to_tile_id(10, 0, 0)
        high = max(
            pmtiles.zxy_to_tile_id(10, x, y)
            for x in (0, 1023)
            for y in (0, 1023)
        )
        assert high - low == (1 << 20) - 1

    def test_a_file_that_is_not_an_archive_is_refused(self, tmp_path) -> None:
        path = tmp_path / "nope.pmtiles"
        path.write_bytes(b"definitely not pmtiles" + bytes(200))
        with pytest.raises(pmtiles.ArchiveError):
            pmtiles.Archive(path)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "gaz"))
    connection = db.open_initialised()
    yield connection
    connection.close()


def add(conn, kind, name, lat, lon, generation=1, category="locality"):
    conn.execute(
        "INSERT INTO gazetteer (name, kind, category, lat, lon, generation) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, kind, category, lat, lon, generation),
    )
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"gazetteer_live_{kind}", str(generation)),
    )
    conn.commit()


class TestLookingNamesUp:
    def test_a_name_is_found(self, conn) -> None:
        add(conn, "place", "Ferrara", 44.837, 11.619)
        found = gazetteer.look_up(conn, "Ferrara", ["place"], 10)
        assert [hit["label"] for hit in found] == ["Ferrara"]

    def test_it_matches_while_still_being_typed(self, conn) -> None:
        add(conn, "place", "Ferrara", 44.837, 11.619)
        assert gazetteer.look_up(conn, "ferr", ["place"], 10)

    def test_a_word_in_the_middle_is_found(self, conn) -> None:
        """"Eleven" has to find "Pizzeria Eleven"."""
        add(conn, "poi", "Pizzeria Eleven", 45.598, 12.883, category="restaurant")
        assert gazetteer.look_up(conn, "eleven", ["poi"], 10)

    def test_accents_are_folded(self, conn) -> None:
        add(conn, "place", "Dörfl", 47.0, 15.0)
        assert gazetteer.look_up(conn, "dorfl", ["place"], 10)

    def test_punctuation_does_not_break_the_query(self, conn) -> None:
        """A stray quote in a search box must not be FTS syntax."""
        add(conn, "poi", "Ai tre tini", 45.598, 12.887, category="pub")
        assert gazetteer.look_up(conn, 'ai "tre', ["poi"], 10) != []

    def test_only_the_kinds_asked_for_come_back(self, conn) -> None:
        add(conn, "place", "Somewhere", 1.0, 1.0)
        add(conn, "poi", "Somewhere", 2.0, 2.0, category="cafe")
        assert len(gazetteer.look_up(conn, "somewhere", ["place"], 10)) == 1

    def test_nothing_is_returned_before_anything_is_built(self, conn) -> None:
        assert gazetteer.look_up(conn, "ferrara", ["place"], 10) == []

    def test_the_view_narrows_it(self, conn) -> None:
        """The reason this option exists: one name, many places."""
        add(conn, "poi", "Eleven", 45.598, 12.883, category="restaurant")
        add(conn, "poi", "Eleven", 48.208, 16.373, category="restaurant", generation=1)
        everywhere = gazetteer.look_up(conn, "eleven", ["poi"], 10)
        near = gazetteer.look_up(conn, "eleven", ["poi"], 10, (12.0, 45.0, 13.5, 46.0))
        assert len(everywhere) == 2
        assert [round(float(hit["lat"]), 3) for hit in near] == [45.598]

    def test_repeats_are_collapsed(self, conn) -> None:
        """Labels are buffered into neighbouring tiles; a few reach the index."""
        for _ in range(4):
            add(conn, "poi", "Ai tre tini", 45.5987, 12.8871, category="pub")
        assert len(gazetteer.look_up(conn, "ai tre tini", ["poi"], 10)) == 1


class TestGenerations:
    def test_only_the_live_generation_answers(self, conn) -> None:
        """A build in progress must not leak into the searches meanwhile."""
        add(conn, "place", "Old name", 1.0, 1.0, generation=1)
        conn.execute(
            "INSERT INTO gazetteer (name, kind, category, lat, lon, generation) "
            "VALUES ('New name', 'place', 'locality', 2.0, 2.0, 2)"
        )
        conn.commit()

        assert [h["label"] for h in gazetteer.look_up(conn, "name", ["place"], 10)] == ["Old name"]

    def test_the_live_generation_is_recorded(self, conn) -> None:
        add(conn, "place", "Ferrara", 44.8, 11.6, generation=7)
        assert gazetteer.live_generation(conn, "place") == 7

    def test_none_built_reads_as_zero(self, conn) -> None:
        assert gazetteer.live_generation(conn, "poi") == 0


class TestRemoving:
    def test_it_takes_every_generation(self, conn) -> None:
        """A stopped build leaves a partial one behind; delete means the disk is back."""
        add(conn, "place", "Kept", 1.0, 1.0, generation=1)
        conn.execute(
            "INSERT INTO gazetteer (name, kind, category, lat, lon, generation) "
            "VALUES ('Partial', 'place', 'locality', 2.0, 2.0, 2)"
        )
        conn.commit()

        gazetteer.remove(conn, "place")
        assert conn.execute("SELECT COUNT(*) AS n FROM gazetteer").fetchone()["n"] == 0

    def test_it_switches_the_search_off_too(self, conn) -> None:
        """Leaving it on with nothing behind it is a search that silently finds nothing."""
        add(conn, "place", "Ferrara", 44.8, 11.6)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('search_place_names', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = 'true'"
        )
        conn.commit()

        gazetteer.remove(conn, "place")
        assert search.included(conn)["place_names"] is False

    def test_an_unknown_kind_is_refused(self, conn) -> None:
        with pytest.raises(KeyError):
            gazetteer.remove(conn, "buildings")


class TestThroughSearch:
    def test_both_are_off_to_begin_with(self, conn) -> None:
        on = search.included(conn)
        assert on["place_names"] is False
        assert on["pois"] is False

    def test_a_name_is_answered_once_switched_on(self, conn) -> None:
        add(conn, "place", "Ferrara", 44.837, 11.619)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('search_place_names', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = 'true'"
        )
        conn.commit()

        answer = search.search(conn, "Ferrara")
        assert [hit["kind"] for hit in answer["results"]] == ["gazetteer"]

    def test_a_pin_of_your_own_comes_first(self, conn) -> None:
        """Somewhere you marked yourself beats a label off a map."""
        add(conn, "place", "Caorle", 45.598, 12.888)
        conn.execute(
            "INSERT INTO places (name, lat, lon, tags) VALUES ('Caorle', 45.6, 12.89, '[]')"
        )
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('search_place_names', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = 'true'"
        )
        conn.commit()

        answer = search.search(conn, "Caorle")
        assert [hit["kind"] for hit in answer["results"]][0] == "pin"

    def test_the_view_narrows_a_search(self, conn) -> None:
        add(conn, "place", "Ferrara", 44.837, 11.619)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('search_place_names', 'true') "
            "ON CONFLICT(key) DO UPDATE SET value = 'true'"
        )
        conn.commit()

        assert search.search(conn, "Ferrara")["results"]
        away = search.search(conn, "Ferrara", None, (16.0, 48.0, 16.5, 48.3))
        assert away["results"] == []
