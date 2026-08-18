# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editing a pin's details must not cost a render.

A title, a label, a tag or who was there changes no pixel: places.update only
re-stamps when the position or the dates move, or when the pin has no event yet.
The endpoint rendered anyway, and worse than that - it passed `dirty or None` as
the scope, and an empty set becomes None, which means "no scope", which means
render these views in full. Correcting a spelling cost a complete re-render of
the cumulative view and every year the pin belonged to.

Measured on a real archive: 0.07s after this, 85s before.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import db
from irfaran.main import app

TOKEN = "synthetic-place-edit-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


@pytest.fixture
def pin(client):
    created = client.post(
        "/api/places",
        headers=auth(),
        json={"name": "Somewhere", "lat": 45.5, "lon": 11.5, "radius_m": 30},
    )
    assert created.status_code == 201
    return created.json()


def tile_mtimes() -> dict[str, float]:
    from irfaran.main import tiles_root

    return {
        str(path): path.stat().st_mtime_ns
        for path in tiles_root().rglob("*.png")
    }


class TestDetailsAreFree:
    @pytest.mark.parametrize(
        "change",
        [
            {"name": "Renamed"},
            {"tags": "one, two"},
            {"people": ["Marie"]},
            {"label_id": None},
            {"folder_id": None},
        ],
        ids=["name", "tags", "people", "label", "folder"],
    )
    def test_no_tile_is_rewritten(self, client, pin, change) -> None:
        before = tile_mtimes()
        response = client.patch(f"/api/places/{pin['id']}", headers=auth(), json=change)
        assert response.status_code == 200

        after = tile_mtimes()
        rewritten = [
            path for path, when in after.items() if before.get(path) != when
        ]
        assert not rewritten, (
            f"changing {list(change)} rewrote {len(rewritten)} tiles; "
            "nothing it changed is drawn on a tile"
        )

    def test_the_change_is_still_saved(self, client, pin) -> None:
        client.patch(
            f"/api/places/{pin['id']}", headers=auth(), json={"name": "Renamed"}
        )
        listing = client.get("/api/places").json()["places"]
        assert any(place["name"] == "Renamed" for place in listing)

    def test_people_survive_the_round_trip(self, client, pin) -> None:
        client.patch(
            f"/api/places/{pin['id']}",
            headers=auth(),
            json={"people": ["Marie", "Jonas"]},
        )
        place = next(
            p for p in client.get("/api/places").json()["places"] if p["id"] == pin["id"]
        )
        assert place["people"] == ["Jonas", "Marie"]


class TestMovingStillCosts:
    def test_a_move_rewrites_tiles(self, client, pin) -> None:
        """The saving must not have been bought by skipping real work."""
        before = tile_mtimes()
        response = client.patch(
            f"/api/places/{pin['id']}",
            headers=auth(),
            json={"lat": 45.51, "lon": 11.51},
        )
        assert response.status_code == 200

        after = tile_mtimes()
        changed = [path for path, when in after.items() if before.get(path) != when]
        added = set(after) - set(before)
        assert changed or added, "moving a pin drew nothing"

    def test_a_move_reports_the_new_position(self, client, pin) -> None:
        moved = client.patch(
            f"/api/places/{pin['id']}",
            headers=auth(),
            json={"lat": 45.52, "lon": 11.52},
        ).json()
        assert moved["lat"] == pytest.approx(45.52)
        assert moved["lon"] == pytest.approx(11.52)
