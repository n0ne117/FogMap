# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which kinds of thing the search bar looks through.

Alex's ask: a toggle per searchable item, with pins the only one on by default.

Coordinates are the deliberate exception, and it is worth writing down why.
Pasting a coordinate is not a search of anything stored - it reads what was
typed - so defaulting it off would silently remove a working feature rather than
quieten a noisy one. It is a toggle like the others for anyone who disagrees.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from irfaran import db, search
from irfaran.main import app

TOKEN = "search-settings-token"


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "settings"))
    connection = db.open_initialised()
    yield connection
    connection.close()


def add_pin(conn, name="Caorle"):
    conn.execute(
        "INSERT INTO places (name, lat, lon, tags) VALUES (?, 45.6, 12.88, '[]')",
        (name,),
    )
    conn.commit()


def add_track(conn, name="Caorle ride"):
    conn.execute(
        "INSERT INTO events (source, op, geometry, radius_m, layers, external_id, "
        "created_at, meta) VALUES ('workout', 'add', ?, 20, '[\"2024\"]', NULL, "
        "'2024-06-01T09:00:00', ?)",
        (
            json.dumps({"type": "LineString", "coordinates": [[11.0, 44.0], [11.1, 44.1]]}),
            json.dumps({"track": name, "fixes": 2}),
        ),
    )
    conn.commit()


def kinds_of(answer) -> set[str]:
    return {str(hit["kind"]) for hit in answer["results"]}


def switch(conn, **values) -> None:
    with db.transaction(conn):
        for key, value in values.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, "true" if value else "false"),
            )


class TestTheDefaults:
    def test_pins_are_on(self, conn) -> None:
        assert search.included(conn)["pins"] is True

    def test_tracks_are_off(self, conn) -> None:
        """The point of the ask: a name search should not answer with segments."""
        assert search.included(conn)["tracks"] is False

    def test_coordinates_are_on(self, conn) -> None:
        """The stated exception. Reading what was typed is not searching a store."""
        assert search.included(conn)["coordinates"] is True

    def test_a_name_finds_the_pin_and_not_the_track(self, conn) -> None:
        add_pin(conn, "Caorle")
        add_track(conn, "Caorle ride")
        assert kinds_of(search.search(conn, "caorle")) == {"pin"}

    def test_a_pasted_coordinate_still_works(self, conn) -> None:
        assert kinds_of(search.search(conn, "27.74367, -15.58338")) == {"coordinates"}


class TestTurningThemOnAndOff:
    def test_tracks_can_be_switched_on(self, conn) -> None:
        add_track(conn, "Caorle ride")
        assert search.search(conn, "caorle")["results"] == []

        switch(conn, search_tracks=True)
        assert kinds_of(search.search(conn, "caorle")) == {"track"}

    def test_pins_can_be_switched_off(self, conn) -> None:
        add_pin(conn, "Caorle")
        switch(conn, search_pins=False)
        assert search.search(conn, "caorle")["results"] == []

    def test_coordinates_can_be_switched_off(self, conn) -> None:
        switch(conn, search_coordinates=False)
        answer = search.search(conn, "27.74367, -15.58338")
        assert answer["results"] == []

    def test_everything_off_finds_nothing_and_says_why(self, conn) -> None:
        add_pin(conn, "Caorle")
        switch(conn, search_pins=False, search_tracks=False, search_coordinates=False)
        answer = search.search(conn, "caorle")
        assert answer["results"] == []
        assert "switched off" in answer["hint"]


class TestSayingWhatIsExcluded:
    def test_a_missing_track_explains_itself(self, conn) -> None:
        """Otherwise a track visible on the map is unfindable for no stated reason."""
        add_track(conn, "Miramare")
        answer = search.search(conn, "miramare")
        assert answer["results"] == []
        assert "Tracks are switched off" in answer["hint"], answer["hint"]

    def test_the_wording_is_plural(self, conn) -> None:
        """"Tracks is switched off" was the first attempt. They are all plurals."""
        add_track(conn, "Miramare")
        assert " is switched off" not in search.search(conn, "miramare")["hint"]

    def test_nothing_is_said_when_everything_is_on(self, conn) -> None:
        switch(conn, search_pins=True, search_tracks=True, search_coordinates=True)
        answer = search.search(conn, "definitely-not-here")
        assert "switched off" not in answer["hint"]


class TestThroughTheApi:
    def test_the_toggles_are_settings_like_any_other(self, conn) -> None:
        with TestClient(app) as client:
            head = {"X-Irfaran-Token": TOKEN}
            assert client.patch(
                "/api/settings", headers=head, json={"search_tracks": "true"}
            ).status_code == 200

            body = client.get("/api/settings").json()
            assert body["settings"]["search_tracks"] == "true"

    def test_a_database_from_before_these_existed_behaves_like_a_new_one(self, conn) -> None:
        """Absent settings must not read as everything switched off."""
        with db.transaction(conn):
            conn.execute("DELETE FROM settings WHERE key LIKE 'search_%'")
        assert search.included(conn) == {
            "pins": True,
            "tracks": False,
            "coordinates": True,
        }

    def test_they_travel_with_an_export(self) -> None:
        from irfaran import transfer

        assert {"search_pins", "search_tracks", "search_coordinates"} <= (
            transfer.PORTABLE_SETTINGS
        )
