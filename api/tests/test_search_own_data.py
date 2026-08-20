# SPDX-License-Identifier: AGPL-3.0-or-later
"""Searching your own pins and tracks.

The basemap cannot be searched - it holds rendered tiles, not an index - so the
useful thing to search is what Irfaran itself knows: the pins somebody placed
and the tracks they imported. Read-only throughout, which is why it needs no
token; keeping a searched coordinate as a pin is the write, and the interface
offers that only when it has credentials.

Two properties here took measuring rather than guessing. Matching folds case in
Python because SQLite only folds ASCII, so `dörfl` would never find `Dörfl` - and
in this archive most names carry an umlaut. And one imported file becomes as many
events as it has gaps in it, up to 37 for a single long ride, so tracks are
grouped by name or a search for one thing answers with forty.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from irfaran import db, search
from irfaran.main import app

TOKEN = "search-data-token"


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "search"))
    connection = db.open_initialised()
    yield connection
    connection.close()


def add_pin(conn, name, *, label=None, folder=None, tags=None, lat=0.3, lon=0.5):
    label_id = None
    if label:
        label_id = conn.execute(
            "INSERT INTO labels (name, colour) VALUES (?, '#402030')", (label,)
        ).lastrowid
    folder_id = None
    if folder:
        folder_id = conn.execute(
            "INSERT INTO folders (name, parent_id, visible) VALUES (?, NULL, 1)", (folder,)
        ).lastrowid
    conn.execute(
        "INSERT INTO places (name, lat, lon, label_id, folder_id, tags) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (name, lat, lon, label_id, folder_id, json.dumps(tags or [])),
    )
    conn.commit()


def add_track(conn, name, *, layer="2024", segments=1, lon=11.0, lat=44.0, created="2024-06-01T09:00:00"):
    """One track, as the ingest writes it: a name in meta, one event per gap."""
    for index in range(segments):
        line = [[lon + index + n * 0.01, lat + index] for n in range(4)]
        conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, "
            "external_id, created_at, meta) VALUES "
            "('workout', 'add', ?, 20, ?, NULL, ?, ?)",
            (
                json.dumps({"type": "LineString", "coordinates": line}),
                json.dumps([layer]),
                created,
                json.dumps({"track": name, "fixes": len(line)}),
            ),
        )
    conn.commit()


def labels_of(answer) -> list[str]:
    return [str(hit["label"]) for hit in answer["results"]]


class TestPins:
    def test_by_title(self, conn) -> None:
        add_pin(conn, "Caorle")
        assert labels_of(search.search(conn, "caorle")) == ["Caorle"]

    def test_by_part_of_a_title(self, conn) -> None:
        add_pin(conn, "Playa del Ingles")
        assert labels_of(search.search(conn, "ingles")) == ["Playa del Ingles"]

    def test_by_label(self, conn) -> None:
        add_pin(conn, "Somewhere", label="Home")
        assert labels_of(search.search(conn, "home")) == ["Somewhere"]

    def test_by_folder(self, conn) -> None:
        add_pin(conn, "Somewhere", folder="Urlaub")
        assert labels_of(search.search(conn, "urlaub")) == ["Somewhere"]

    def test_by_tag(self, conn) -> None:
        add_pin(conn, "Somewhere", tags=["beach", "windy"])
        assert labels_of(search.search(conn, "windy")) == ["Somewhere"]

    def test_the_row_says_why_it_matched(self, conn) -> None:
        """A hit on a tag looks arbitrary unless the tag is on screen."""
        add_pin(conn, "Somewhere", label="Home", tags=["beach"])
        detail = str(search.search(conn, "beach")["results"][0]["detail"])
        assert "Home" in detail or "beach" in detail

    def test_it_carries_somewhere_to_go(self, conn) -> None:
        add_pin(conn, "Caorle", lat=45.6, lon=12.88)
        hit = search.search(conn, "caorle")["results"][0]
        assert hit["lat"] == pytest.approx(45.6)
        assert hit["lon"] == pytest.approx(12.88)

    def test_a_leading_match_comes_first(self, conn) -> None:
        """Typing "cao" wants Caorle, not a pin that mentions it in passing."""
        add_pin(conn, "Zzz somewhere", tags=["caorle"])
        add_pin(conn, "Caorle")
        assert labels_of(search.search(conn, "cao"))[0] == "Caorle"


class TestCaseAndAccents:
    def test_lowercase_finds_an_umlaut(self, conn) -> None:
        """SQLite's LIKE folds ASCII only, which is why this is done in Python."""
        add_track(conn, "Veleniki -> Dörfl (394.4km)")
        assert search.search(conn, "dörfl")["results"], "an umlaut was not folded"

    def test_uppercase_finds_lowercase(self, conn) -> None:
        add_track(conn, "commute")
        assert search.search(conn, "COMMUTE")["results"]

    def test_a_pin_folds_the_same_way(self, conn) -> None:
        add_pin(conn, "Wien Süd")
        assert labels_of(search.search(conn, "süd")) == ["Wien Süd"]


class TestTracks:
    def test_by_name(self, conn) -> None:
        add_track(conn, "Miramare (420.4km)")
        assert labels_of(search.search(conn, "miramare")) == ["Miramare (420.4km)"]

    def test_the_segments_of_one_file_are_one_result(self, conn) -> None:
        """37 events for one ride must not be 37 answers."""
        add_track(conn, "Favoriten -> Florence", segments=12)
        found = search.search(conn, "florence")["results"]
        assert len(found) == 1
        assert "12 segments" in str(found[0]["detail"])

    def test_a_single_segment_says_nothing_about_segments(self, conn) -> None:
        add_track(conn, "Dörfl -> Pacher", segments=1)
        assert "segment" not in str(search.search(conn, "pacher")["results"][0]["detail"])

    def test_it_carries_a_box_to_fit(self, conn) -> None:
        """A track is a shape, not a point, so the map can frame the whole thing."""
        add_track(conn, "Long one", segments=3, lon=11.0, lat=44.0)
        hit = search.search(conn, "long one")["results"][0]
        (west, south), (east, north) = hit["bounds"]
        assert west < east and south < north
        assert west == pytest.approx(11.0)
        assert north == pytest.approx(46.0)

    def test_a_track_with_no_usable_geometry_is_left_out(self, conn) -> None:
        """Nowhere to go is not a search result."""
        conn.execute(
            "INSERT INTO events (source, op, geometry, radius_m, layers, "
            "external_id, created_at, meta) VALUES "
            "('workout', 'add', 'not json', 20, '[\"2024\"]', NULL, "
            "'2024-06-01T09:00:00', ?)",
            (json.dumps({"track": "Broken"}),),
        )
        conn.commit()
        assert search.search(conn, "broken")["results"] == []


class TestByYear:
    """The year is the only date a track has, and the reason is worth knowing.

    `created_at` on an event is when it was imported - `datetime.now()` in the
    ingest, for every source. The activity's own date survives only as the year,
    in `layers`, worked out from the fixes' timestamps. So a month can look like
    it works while answering about the day somebody uploaded a file.
    """

    def test_a_year_finds_what_is_filed_under_it(self, conn) -> None:
        add_track(conn, "Ride", layer="2024")
        add_track(conn, "Older", layer="2019")
        assert labels_of(search.search(conn, "2024")) == ["Ride"]

    def test_the_year_comes_from_the_layer_not_the_import_date(self, conn) -> None:
        add_track(conn, "Walked in 1994", layer="1994", created="2026-08-19T12:00:00")
        assert labels_of(search.search(conn, "1994")) == ["Walked in 1994"]
        assert labels_of(search.search(conn, "2026")) == [], (
            "a track imported in 2026 was offered as a track from 2026"
        )

    def test_prehistory_belongs_to_no_year(self, conn) -> None:
        add_track(conn, "Before GPS", layer="prehistory", created="2026-08-19T12:00:00")
        assert labels_of(search.search(conn, "2026")) == []
        assert labels_of(search.search(conn, "before")) == ["Before GPS"]

    def test_a_finer_date_explains_itself(self, conn) -> None:
        """Rather than quietly matching the day a file was uploaded."""
        add_track(conn, "June ride", layer="2024", created="2024-06-11T09:00:00")
        answer = search.search(conn, "2024-06")
        assert answer["results"] == []
        assert "Only the year" in answer["hint"]
        assert "2024" in answer["hint"]

    def test_a_finer_date_still_matches_a_name(self, conn) -> None:
        """Live sources name their tracks after a timestamp, so this is a name."""
        add_track(conn, "2024-12-31T18:23:26Z", layer="2024")
        assert labels_of(search.search(conn, "2024-12")) == ["2024-12-31T18:23:26Z"]

    def test_the_detail_says_which_year(self, conn) -> None:
        add_track(conn, "Ride", layer="2024")
        assert "2024" in str(search.search(conn, "ride")["results"][0]["detail"])

    def test_a_track_spanning_years_says_the_span(self, conn) -> None:
        add_track(conn, "New Year ride", layer="2023")
        add_track(conn, "New Year ride", layer="2024")
        detail = str(search.search(conn, "new year")["results"][0]["detail"])
        assert "2023" in detail and "2024" in detail


class TestHowMuchItAnswersWith:
    def test_it_stops_at_the_limit(self, conn) -> None:
        for index in range(search.LIMIT + 6):
            add_track(conn, f"Ride {index}", created=f"2024-06-{index % 28 + 1:02d}T09:00:00")
        answer = search.search(conn, "ride")
        assert len(answer["results"]) == search.LIMIT

    def test_it_says_how_many_it_left_out(self, conn) -> None:
        for index in range(search.LIMIT + 6):
            add_track(conn, f"Ride {index}")
        answer = search.search(conn, "ride")
        assert f"of {search.LIMIT + 6}" in answer["hint"], answer["hint"]

    def test_nothing_found_says_what_is_searchable(self, conn) -> None:
        answer = search.search(conn, "definitely-not-here")
        assert answer["results"] == []
        assert "name" in answer["hint"] and "year" in answer["hint"]


class TestItStaysReadOnly:
    def test_searching_needs_no_token(self, conn) -> None:
        add_pin(conn, "Caorle")
        with TestClient(app) as client:
            body = client.get("/api/search", params={"q": "caorle"}).json()
        assert [hit["label"] for hit in body["results"]] == ["Caorle"]

    def test_searching_writes_nothing(self, conn) -> None:
        """Read-only is the reason it needs no token, so it has to be true."""
        add_pin(conn, "Caorle")
        add_track(conn, "Ride")
        before = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("events", "places", "blobs", "pending_render", "log")
        }

        with TestClient(app) as client:
            for query in ("caorle", "ride", "2024", "27.7, -15.5", "nonsense"):
                client.get("/api/search", params={"q": query})

        after = {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("events", "places", "blobs", "pending_render", "log")
        }
        assert before == after, f"searching changed the database: {before} -> {after}"


class TestGeometryIsNotReadForEverything:
    def test_only_the_results_returned_have_their_geometry_read(self, conn) -> None:
        """Measured at 27.8 MB a search before this: cost set by how much has
        been walked rather than by what was asked for, which is the shape of the
        scan that made every render slow before 0.17.6."""
        for index in range(search.LIMIT + 20):
            add_track(conn, f"Ride {index}", created=f"2024-06-{index % 28 + 1:02d}T09:00:00")

        reads = {"n": 0}
        real = search._bounds

        def counted(connection, ids):
            reads["n"] += 1
            return real(connection, ids)

        search._bounds = counted  # type: ignore[assignment]
        try:
            answer = search.search(conn, "ride")
        finally:
            search._bounds = real  # type: ignore[assignment]

        assert len(answer["results"]) == search.LIMIT
        assert reads["n"] <= search.LIMIT, (
            f"geometry was read for {reads['n']} tracks to answer with "
            f"{len(answer['results'])}"
        )


class TestTheInterfaceGate:
    """Searching is read-only; keeping a result is not.

    Alex's rule, and it draws the line in the right place: a coordinate is found,
    flown to and marked without credentials, because none of that changes
    anything. The offer to keep it as a pin is a write, so it appears only when
    there is a token to make it with - rather than a button that fails when
    pressed.

    Source-level, because the TypeScript has no test runner. See
    test_progress_notice.py for the same reasoning.
    """

    def client_source(self) -> str:
        from .test_progress_notice import source

        return source("search.ts")

    def test_the_offer_is_gated_on_a_token(self) -> None:
        from .test_progress_notice import body_of

        body = body_of(self.client_source(), "private offer(")
        assert "getToken()" in body, (
            "the save offer is not gated on a token, so it will be shown to "
            "somebody who cannot use it"
        )

    def test_it_says_where_to_put_the_token(self) -> None:
        """A disabled offer with no explanation is a dead end."""
        from .test_progress_notice import body_of

        body = body_of(self.client_source(), "private offer(")
        assert "Settings" in body

    def test_the_marker_is_dropped_either_way(self) -> None:
        """The read-only half has to keep working without credentials."""
        from .test_progress_notice import body_of

        body = body_of(self.client_source(), "private drop(")
        assert "getToken" not in body, (
            "dropping the marker is read-only and must not depend on a token"
        )

    def test_searching_does_not_ask_for_a_token(self) -> None:
        from .test_progress_notice import body_of

        body = body_of(self.client_source(), "private async run(")
        assert "getToken" not in body
