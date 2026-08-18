# SPDX-License-Identifier: AGPL-3.0-or-later
"""Major and minor pins.

The two differ in exactly two ways: a minor pin is drawn smaller, and it stops
being drawn once the map is further out than the threshold, where a valley with
three pins in it is one smudge. Everything else about them is identical, which
is why this is one column and not a second kind of thing.

Defaulted to major, so every pin that existed before this stays as prominent as
it was - a migration that quietly demoted somebody's pins would be worse than
not having the feature.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import db, places
from irfaran.main import app

TOKEN = "synthetic-prominence-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


def drop(client, name: str, **extra) -> dict:
    body = {"name": name, "lat": 44.1, "lon": 10.1, "radius_m": 30, **extra}
    created = client.post("/api/places", headers=auth(), json=body)
    assert created.status_code == 201, created.text
    return created.json()


class TestTheDefault:
    def test_a_new_pin_is_major(self, client) -> None:
        assert drop(client, "Unspecified")["prominence"] == "major"

    def test_an_existing_row_without_the_column_reads_as_major(self) -> None:
        """What the migration does to pins that predate it."""
        conn = db.connect()
        try:
            cursor = conn.execute(
                "INSERT INTO places (name, lat, lon) VALUES ('Old', 45.0, 12.0)"
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM places WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
            assert places.as_dict(row)["prominence"] == "major"
        finally:
            conn.close()


class TestSettingIt:
    def test_a_pin_can_be_minor_from_the_start(self, client) -> None:
        assert drop(client, "Small", prominence="minor")["prominence"] == "minor"

    def test_it_can_be_changed_later(self, client) -> None:
        pin = drop(client, "Changeable")
        changed = client.patch(
            f"/api/places/{pin['id']}", headers=auth(), json={"prominence": "minor"}
        )
        assert changed.status_code == 200
        assert changed.json()["prominence"] == "minor"

    def test_changing_it_draws_nothing(self, client) -> None:
        """Prominence is a viewing choice. It must not cost a render."""
        from irfaran.main import tiles_root

        pin = drop(client, "Free to change")
        before = {
            str(path): path.stat().st_mtime_ns for path in tiles_root().rglob("*.png")
        }
        client.patch(
            f"/api/places/{pin['id']}", headers=auth(), json={"prominence": "minor"}
        )
        after = {
            str(path): path.stat().st_mtime_ns for path in tiles_root().rglob("*.png")
        }
        rewritten = [p for p, when in after.items() if before.get(p) != when]
        assert not rewritten, f"changing prominence rewrote {len(rewritten)} tiles"

    def test_nonsense_is_refused(self, client) -> None:
        response = client.post(
            "/api/places",
            headers=auth(),
            json={"name": "Odd", "lat": 44.0, "lon": 10.0, "prominence": "enormous"},
        )
        assert response.status_code == 400
        assert "prominence must be one of" in response.json()["detail"]

    def test_case_and_spacing_are_forgiven(self, client) -> None:
        assert drop(client, "Shouty", prominence="  MINOR ")["prominence"] == "minor"

    def test_an_omitted_value_keeps_what_was_there(self, client) -> None:
        """A save that does not mention prominence must not reset it."""
        pin = drop(client, "Keeps it", prominence="minor")
        renamed = client.patch(
            f"/api/places/{pin['id']}", headers=auth(), json={"name": "Still minor"}
        )
        assert renamed.json()["prominence"] == "minor"


class TestItTravels:
    def test_prominence_survives_an_export(self, client) -> None:
        import io
        import zipfile

        drop(client, "Exported", prominence="minor")
        archive = client.get("/api/export", headers=auth())
        with zipfile.ZipFile(io.BytesIO(archive.content)) as zipped:
            assert "minor" in zipped.read("places.json").decode()
