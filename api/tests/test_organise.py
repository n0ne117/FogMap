# SPDX-License-Identifier: AGPL-3.0-or-later
"""Labels and folders: the two things pins are sorted by."""

from __future__ import annotations

import pytest

from fogmap import db, organise, places


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "fogmap.db")
    yield connection
    connection.close()


def a_place(conn, name="Somewhere", **extra):
    payload = {"name": name, "lat": 0.3, "lon": 0.6, **extra}
    place, _, _ = places.create(conn, payload)
    return place


class TestLabels:
    def test_a_label_keeps_its_name_and_colour(self, conn):
        label = organise.create_label(conn, {"name": "Home", "colour": "#402030"})
        assert label["name"] == "Home"
        assert label["colour"] == "#402030"

    def test_a_short_hex_is_expanded(self, conn):
        assert organise.create_label(conn, {"name": "X", "colour": "#f0a"})[
            "colour"
        ] == "#ff00aa"

    def test_no_colour_means_the_default(self, conn):
        label = organise.create_label(conn, {"name": "Plain"})
        assert label["colour"] == organise.DEFAULT_COLOUR

    def test_a_nameless_label_is_refused_by_name(self, conn):
        with pytest.raises(organise.OrganiseError, match="A label needs a name"):
            organise.create_label(conn, {"colour": "#ffffff"})

    def test_a_bad_colour_is_refused_by_name(self, conn):
        with pytest.raises(organise.OrganiseError, match="hex colour"):
            organise.create_label(conn, {"name": "X", "colour": "tangerine"})

    def test_two_labels_cannot_share_a_name(self, conn):
        organise.create_label(conn, {"name": "Home"})
        with pytest.raises(organise.OrganiseError, match="already a label called"):
            organise.create_label(conn, {"name": "Home"})

    def test_renaming_and_recolouring(self, conn):
        label = organise.create_label(conn, {"name": "Hom", "colour": "#111111"})
        changed = organise.update_label(
            conn, label["id"], {"name": "Home", "colour": "#222222"}
        )
        assert (changed["name"], changed["colour"]) == ("Home", "#222222")

    def test_deleting_a_label_keeps_the_pins_that_wore_it(self, conn):
        label = organise.create_label(conn, {"name": "Home"})
        place = a_place(conn, label_id=label["id"])
        assert place["label_id"] == label["id"]

        organise.delete_label(conn, label["id"])

        rows = places.listing(conn)
        assert len(rows) == 1, "deleting a label must not delete anywhere you have been"
        assert rows[0]["label_id"] is None

    def test_an_unknown_label_is_a_key_error(self, conn):
        with pytest.raises(KeyError):
            organise.update_label(conn, 404, {"name": "Nope"})


class TestFolders:
    def test_a_folder_starts_visible(self, conn):
        folder = organise.create_folder(conn, {"name": "Austria"})
        assert folder["visible"] is True
        assert folder["parent_id"] is None

    def test_a_folder_can_hold_a_folder(self, conn):
        top = organise.create_folder(conn, {"name": "Austria"})
        child = organise.create_folder(conn, {"name": "Graz", "parent_id": top["id"]})
        assert child["parent_id"] == top["id"]
        assert organise.depth_of(conn, child["id"]) == 1

    def test_nesting_stops_at_two_deep(self, conn):
        top = organise.create_folder(conn, {"name": "Austria"})
        child = organise.create_folder(conn, {"name": "Graz", "parent_id": top["id"]})
        with pytest.raises(organise.OrganiseError, match="as far in as they go"):
            organise.create_folder(conn, {"name": "Deeper", "parent_id": child["id"]})

    def test_an_unknown_parent_is_refused_by_name(self, conn):
        with pytest.raises(organise.OrganiseError, match="no folder with id 99"):
            organise.create_folder(conn, {"name": "Orphan", "parent_id": 99})

    def test_a_folder_cannot_be_inside_itself(self, conn):
        folder = organise.create_folder(conn, {"name": "Austria"})
        with pytest.raises(organise.OrganiseError, match="inside itself"):
            organise.update_folder(conn, folder["id"], {"parent_id": folder["id"]})

    def test_hiding_a_folder(self, conn):
        folder = organise.create_folder(conn, {"name": "Austria"})
        hidden = organise.update_folder(conn, folder["id"], {"visible": False})
        assert hidden["visible"] is False
        assert organise.update_folder(conn, folder["id"], {"visible": True})["visible"]

    def test_deleting_a_folder_takes_its_subfolders_but_not_its_pins(self, conn):
        top = organise.create_folder(conn, {"name": "Austria"})
        child = organise.create_folder(conn, {"name": "Graz", "parent_id": top["id"]})
        a_place(conn, "Flat", folder_id=child["id"])
        a_place(conn, "Park", folder_id=top["id"])

        organise.delete_folder(conn, top["id"])

        assert organise.folders(conn) == []
        rows = places.listing(conn)
        assert len(rows) == 2, "deleting a folder is a filing decision, not a delete"
        assert all(row["folder_id"] is None for row in rows)

    def test_an_unknown_folder_is_a_key_error(self, conn):
        with pytest.raises(KeyError):
            organise.delete_folder(conn, 404)


class TestPlacesReferToThem:
    def test_a_pin_records_its_label_folder_and_tags(self, conn):
        label = organise.create_label(conn, {"name": "Home"})
        folder = organise.create_folder(conn, {"name": "Austria"})

        place = a_place(
            conn,
            "Grandmothers house",
            label_id=label["id"],
            folder_id=folder["id"],
            tags="childhood, summer, childhood",
        )

        assert place["label_id"] == label["id"]
        assert place["folder_id"] == folder["id"]
        # De-duplicated and sorted, so the same tag typed twice is one tag.
        assert place["tags"] == ["childhood", "summer"]

    def test_a_pin_pointing_at_nothing_is_refused_by_name(self, conn):
        with pytest.raises(places.PlaceError, match="no folder with id 77"):
            a_place(conn, folder_id=77)
        with pytest.raises(places.PlaceError, match="no label with id 88"):
            a_place(conn, label_id=88)

    def test_a_pin_clears_fog_without_leaving_a_track(self, conn):
        """Somebody was here is not the same claim as somebody walked here."""
        a_place(conn)

        event = conn.execute(
            "SELECT * FROM events WHERE source = 'place'"
        ).fetchone()
        assert event["op"] == "reveal"
        assert event["radius_m"] == 30.0

        trails = conn.execute(
            "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'trail'"
        ).fetchone()["n"]
        fog = conn.execute(
            "SELECT COUNT(*) AS n FROM blobs WHERE kind = 'fog'"
        ).fetchone()["n"]
        assert fog > 0
        assert trails == 0

    def test_moving_a_pin_reports_both_ends_as_dirty(self, conn):
        place = a_place(conn)
        _, _, _, dirty = places.update(conn, place["id"], {"lat": 0.9, "lon": 12.0})
        assert len(dirty) >= 2, "the ground it left and the ground it arrived at"


class TestMigration:
    """The columns were added to a table that already existed."""

    def test_init_adds_the_columns_to_an_older_database(self, tmp_path):
        import sqlite3

        path = tmp_path / "old.db"
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE places (id INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "category TEXT, people TEXT, date_from TEXT, date_to TEXT, "
            "lat REAL NOT NULL, lon REAL NOT NULL, event_id INTEGER)"
        )
        old.execute(
            "INSERT INTO places (name, lat, lon) VALUES ('Older place', 0.3, 0.6)"
        )
        old.commit()
        old.close()

        conn = db.open_initialised(path)
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(places)")}
            assert {"label_id", "folder_id", "tags"} <= columns

            rows = places.listing(conn)
            assert len(rows) == 1, "the row that was already there is still there"
            assert rows[0]["name"] == "Older place"
            assert rows[0]["tags"] == []
        finally:
            conn.close()

    def test_running_init_twice_changes_nothing(self, tmp_path):
        conn = db.open_initialised(tmp_path / "twice.db")
        try:
            db.init(conn)
            db.init(conn)
            columns = [row["name"] for row in conn.execute("PRAGMA table_info(places)")]
            assert len(columns) == len(set(columns))
        finally:
            conn.close()
