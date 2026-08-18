# SPDX-License-Identifier: AGPL-3.0-or-later
"""The who-was-there registry.

A place stores the names themselves, not ids pointing here, which is the whole
design: a pin keeps who was there even after the name is taken off the list, and
a backup carries the names without needing the registry to survive the journey.

The consequence is that a rename has to reach the pins too, or the list and the
pins disagree and the pins look like data loss.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from irfaran import db, organise
from irfaran.main import app

TOKEN = "synthetic-people-token"


@pytest.fixture
def conn():
    connection = db.open_initialised()
    connection.execute("DELETE FROM people")
    connection.execute("DELETE FROM places")
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def client(monkeypatch, conn):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


def a_place(conn, name: str, people: list[str]) -> int:
    cursor = conn.execute(
        "INSERT INTO places (name, lat, lon, people) VALUES (?, ?, ?, ?)",
        (name, 45.0, 12.0, json.dumps(people)),
    )
    conn.commit()
    return int(cursor.lastrowid)


class TestTheRegistry:
    def test_somebody_can_be_added(self, conn) -> None:
        person = organise.create_person(conn, {"name": "Marie"})
        assert person["name"] == "Marie"
        assert [p["name"] for p in organise.people(conn)] == ["Marie"]

    def test_a_name_is_required(self, conn) -> None:
        with pytest.raises(organise.OrganiseError, match="needs a name"):
            organise.create_person(conn, {"name": "   "})

    def test_the_same_name_twice_is_refused(self, conn) -> None:
        organise.create_person(conn, {"name": "Marie"})
        with pytest.raises(organise.OrganiseError, match="already on the list"):
            organise.create_person(conn, {"name": "Marie"})

    def test_case_does_not_make_a_second_person(self, conn) -> None:
        """"marie" and "Marie" are the same person, and the point of a registry
        is that the same person is spelled one way."""
        organise.create_person(conn, {"name": "Marie"})
        with pytest.raises(organise.OrganiseError):
            organise.create_person(conn, {"name": "marie"})

    def test_they_come_back_in_name_order(self, conn) -> None:
        for name in ("Zoe", "adam", "Marie"):
            organise.create_person(conn, {"name": name})
        assert [p["name"] for p in organise.people(conn)] == ["adam", "Marie", "Zoe"]


class TestRenaming:
    def test_a_rename_reaches_the_pins(self, conn) -> None:
        person = organise.create_person(conn, {"name": "Marie"})
        place = a_place(conn, "Beach", ["Marie", "Jonas"])

        organise.update_person(conn, int(person["id"]), {"name": "Marie K"})

        stored = json.loads(
            conn.execute("SELECT people FROM places WHERE id = ?", (place,)).fetchone()[
                "people"
            ]
        )
        assert "Marie K" in stored
        assert "Marie" not in stored
        assert "Jonas" in stored, "it renamed somebody else too"

    def test_a_rename_does_not_duplicate_an_existing_name(self, conn) -> None:
        """Renaming Marie to Jonas on a pin that has both leaves one Jonas."""
        person = organise.create_person(conn, {"name": "Marie"})
        place = a_place(conn, "Beach", ["Marie", "Jonas"])
        organise.update_person(conn, int(person["id"]), {"name": "Jonas"})

        stored = json.loads(
            conn.execute("SELECT people FROM places WHERE id = ?", (place,)).fetchone()[
                "people"
            ]
        )
        assert stored.count("Jonas") == 1

    def test_a_clash_is_refused(self, conn) -> None:
        organise.create_person(conn, {"name": "Marie"})
        jonas = organise.create_person(conn, {"name": "Jonas"})
        with pytest.raises(organise.OrganiseError, match="already on the list"):
            organise.update_person(conn, int(jonas["id"]), {"name": "Marie"})

    def test_renaming_somebody_who_is_not_there(self, conn) -> None:
        with pytest.raises(KeyError):
            organise.update_person(conn, 999, {"name": "Nobody"})


class TestRemoving:
    def test_the_pins_keep_the_name(self, conn) -> None:
        """Taking a name off the list means stop offering it, not forget them."""
        person = organise.create_person(conn, {"name": "Marie"})
        place = a_place(conn, "Beach", ["Marie"])

        organise.delete_person(conn, int(person["id"]))

        stored = json.loads(
            conn.execute("SELECT people FROM places WHERE id = ?", (place,)).fetchone()[
                "people"
            ]
        )
        assert stored == ["Marie"]
        assert organise.people(conn) == []

    def test_removing_somebody_who_is_not_there(self, conn) -> None:
        with pytest.raises(KeyError):
            organise.delete_person(conn, 999)


class TestEndpoint:
    def test_the_list_is_readable_without_a_token(self, client) -> None:
        assert client.get("/api/people").status_code == 200

    def test_adding_needs_the_token(self, client) -> None:
        assert client.post("/api/people", json={"name": "Marie"}).status_code in (401, 403)

    def test_the_round_trip(self, client) -> None:
        created = client.post("/api/people", headers=auth(), json={"name": "Marie"})
        assert created.status_code == 201
        person_id = created.json()["id"]

        assert client.patch(
            f"/api/people/{person_id}", headers=auth(), json={"name": "Marie K"}
        ).status_code == 200
        assert client.get("/api/people").json()["people"][0]["name"] == "Marie K"
        assert client.delete(f"/api/people/{person_id}", headers=auth()).status_code == 200
        assert client.get("/api/people").json()["people"] == []

    def test_it_reports_names_used_on_pins_as_well(self, client, conn) -> None:
        """A pin from an older backup can name somebody never registered here."""
        a_place(conn, "Beach", ["Stranger"])
        body = client.get("/api/people").json()
        assert body["people"] == []
        assert body["named_on_pins"] == ["Stranger"]

    def test_a_missing_person_is_a_404(self, client) -> None:
        assert client.patch(
            "/api/people/999", headers=auth(), json={"name": "x"}
        ).status_code == 404
        assert client.delete("/api/people/999", headers=auth()).status_code == 404

    def test_a_duplicate_is_a_409_or_400_not_a_500(self, client) -> None:
        client.post("/api/people", headers=auth(), json={"name": "Marie"})
        again = client.post("/api/people", headers=auth(), json={"name": "Marie"})
        assert again.status_code in (400, 409)
        assert "already on the list" in again.json()["detail"]


class TestBackup:
    def test_people_travel_with_an_export(self, client, conn) -> None:
        import io
        import zipfile

        organise.create_person(conn, {"name": "Marie"})
        conn.commit()

        archive = client.get("/api/export", headers=auth())
        assert archive.status_code == 200
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            assert "people.json" in zipped.namelist()
            assert "Marie" in zipped.read("people.json").decode()
