# SPDX-License-Identifier: AGPL-3.0-or-later
"""Folders inside folders.

The capability was always there and nothing in the interface said so: the only
way to set a parent was a picker that appeared while dropping a pin, so making a
subfolder meant dropping a pin you did not want. These check the server side of
what the plus button now does.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import db, organise
from irfaran.main import app

TOKEN = "synthetic-folder-token"


@pytest.fixture
def conn():
    connection = db.open_initialised()
    connection.execute("DELETE FROM folders")
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


class TestNesting:
    def test_a_folder_goes_inside_another(self, conn) -> None:
        year = organise.create_folder(conn, {"name": "2026"})
        trip = organise.create_folder(
            conn, {"name": "Urlaub Caorle", "parent_id": year["id"]}
        )
        assert trip["parent_id"] == year["id"]
        assert organise.depth_of(conn, int(trip["id"])) == 1

    def test_a_top_level_folder_has_no_parent(self, conn) -> None:
        year = organise.create_folder(conn, {"name": "2026"})
        assert year["parent_id"] is None
        assert organise.depth_of(conn, int(year["id"])) == 0

    def test_the_third_level_is_refused_with_a_reason(self, conn) -> None:
        year = organise.create_folder(conn, {"name": "2026"})
        trip = organise.create_folder(
            conn, {"name": "Urlaub Caorle", "parent_id": year["id"]}
        )
        with pytest.raises(organise.OrganiseError, match="as far in as they go"):
            organise.create_folder(conn, {"name": "Day one", "parent_id": trip["id"]})

    def test_an_unknown_parent_is_refused(self, conn) -> None:
        with pytest.raises(organise.OrganiseError, match="no folder with id"):
            organise.create_folder(conn, {"name": "Orphan", "parent_id": 999})

    def test_an_existing_folder_can_be_moved_under_another(self, conn) -> None:
        """What somebody who made two top-level folders needs, having wanted
        one inside the other."""
        year = organise.create_folder(conn, {"name": "2026"})
        trip = organise.create_folder(conn, {"name": "Urlaub Caorle"})
        assert trip["parent_id"] is None

        moved = organise.update_folder(
            conn, int(trip["id"]), {"parent_id": year["id"]}
        )
        assert moved["parent_id"] == year["id"]


class TestEndpoint:
    def test_creating_a_subfolder_over_http(self, client) -> None:
        year = client.post("/api/folders", headers=auth(), json={"name": "2026"}).json()
        trip = client.post(
            "/api/folders",
            headers=auth(),
            json={"name": "Urlaub Caorle", "parent_id": year["id"]},
        )
        assert trip.status_code == 201
        assert trip.json()["parent_id"] == year["id"]

    def test_too_deep_is_a_client_error_not_a_500(self, client) -> None:
        year = client.post("/api/folders", headers=auth(), json={"name": "2026"}).json()
        trip = client.post(
            "/api/folders",
            headers=auth(),
            json={"name": "Trip", "parent_id": year["id"]},
        ).json()
        third = client.post(
            "/api/folders",
            headers=auth(),
            json={"name": "Day", "parent_id": trip["id"]},
        )
        assert third.status_code in (400, 409)
        assert "as far in as they go" in third.json()["detail"]
