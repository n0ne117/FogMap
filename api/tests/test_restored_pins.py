# SPDX-License-Identifier: AGPL-3.0-or-later
"""Editing a pin that arrived in a restore.

Reported from a fresh install: changing a pin's label answered 500, on data
imported from another instance where the same edit worked. Nothing else failed,
which is the shape of a problem in the imported rows rather than in the edit.

A pin's fog is an event, and `places.event_id` is the link between them. The
merge inserted pins without that link, so every imported pin looked like a pin
whose fog had never been stamped - and `update` re-stamps when it sees that.
Re-stamping inserts an event with `external_id = "place-<id>"`, the archive had
already brought one with that exact pair, and there is a UNIQUE index across
(source, external_id). Hence a 500 on the first edit of any restored pin.

The link has to survive a restore, and re-stamping has to be safe even where it
has not - a database that was already imported by an older version still carries
pins with no link, and those must not be a landmine.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import db
from irfaran.main import app

SOURCE_TOKEN = "restore-source-token"
TARGET_TOKEN = "restore-target-token"


@pytest.fixture
def source(monkeypatch, tmp_path):
    monkeypatch.setenv("IRFARAN_TOKEN", SOURCE_TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "source"))
    with TestClient(app) as client:
        head = {"X-Irfaran-Token": SOURCE_TOKEN}
        client.post("/api/labels", headers=head, json={"name": "Home", "colour": "#402030"})
        client.post("/api/folders", headers=head, json={"name": "Austria"})
        client.post(
            "/api/places",
            headers=head,
            json={"name": "A place", "lat": 0.31, "lon": 0.52},
        )
        client.post(
            "/api/places",
            headers=head,
            json={"name": "Another place", "lat": 0.42, "lon": 0.63},
        )
        yield client


def restore(monkeypatch, tmp_path, payload: bytes) -> TestClient:
    monkeypatch.setenv("IRFARAN_TOKEN", TARGET_TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "target"))
    client = TestClient(app)
    client.__enter__()
    result = client.post(
        "/api/import",
        headers={"X-Irfaran-Token": TARGET_TOKEN},
        files={"file": ("backup.irfaran", payload, "application/zip")},
    )
    assert result.status_code == 200, result.text
    return client


@pytest.fixture
def restored(source, monkeypatch, tmp_path):
    payload = source.get(
        "/api/export", headers={"X-Irfaran-Token": SOURCE_TOKEN}
    ).content
    client = restore(monkeypatch, tmp_path, payload)
    yield client
    client.__exit__(None, None, None)


def head() -> dict[str, str]:
    return {"X-Irfaran-Token": TARGET_TOKEN}


def pins(client) -> list[dict]:
    return client.get("/api/places").json()["places"]


class TestEditingARestoredPin:
    def test_changing_its_label_works(self, restored) -> None:
        """The reported failure, verbatim: a label change and nothing else."""
        pin = pins(restored)[0]
        label = restored.get("/api/labels").json()["labels"][0]

        response = restored.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"label_id": label["id"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["label_id"] == label["id"]

    def test_renaming_it_works(self, restored) -> None:
        pin = pins(restored)[0]
        response = restored.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"name": "Renamed"}
        )
        assert response.status_code == 200, response.text

    def test_every_restored_pin_can_be_edited(self, restored) -> None:
        """One working pin proves little when ids happen to line up."""
        for pin in pins(restored):
            response = restored.patch(
                f"/api/places/{pin['id']}", headers=head(), json={"tags": "checked"}
            )
            assert response.status_code == 200, f"pin {pin['id']}: {response.text}"


class TestTheLinkSurvivesARestore:
    def test_a_restored_pin_knows_its_event(self, restored) -> None:
        """Without this, the first edit of every pin re-stamps its fog."""
        conn = db.connect()
        try:
            rows = conn.execute(
                "SELECT id, name, event_id FROM places ORDER BY id"
            ).fetchall()
            assert rows, "the restore brought no pins"
            orphans = [row["name"] for row in rows if row["event_id"] is None]
            assert not orphans, f"restored pins with no event: {orphans}"
        finally:
            conn.close()

    def test_editing_does_not_duplicate_the_fog_event(self, restored) -> None:
        """Two events for one pin means fog cleared twice and undone once."""
        pin = pins(restored)[0]
        restored.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"name": "Edited once"}
        )
        restored.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"name": "Edited twice"}
        )

        conn = db.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE source = 'place'"
            ).fetchone()["n"]
        finally:
            conn.close()
        assert count == len(pins(restored)), (
            f"{count} place events for {len(pins(restored))} pins"
        )


class TestADatabaseAlreadyInThatState:
    """The instance that has already been restored by a version without the link.

    Fixing the import helps the next restore. It does nothing for a database
    sitting there now with pins that have no event_id and events named after
    them - which is exactly the machine that reported this. Editing a pin there
    has to work, and has to leave one event per pin rather than two.
    """

    @pytest.fixture
    def orphaned(self, restored):
        conn = db.connect()
        try:
            with db.transaction(conn):
                conn.execute("UPDATE places SET event_id = NULL")
        finally:
            conn.close()
        return restored

    def test_the_link_is_really_gone(self, orphaned) -> None:
        """Otherwise the rest of this class proves nothing."""
        conn = db.connect()
        try:
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM places WHERE event_id IS NOT NULL"
                ).fetchone()["n"]
                == 0
            )
            assert (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM events WHERE source = 'place'"
                ).fetchone()["n"]
                > 0
            ), "no leftover events, so nothing to collide with"
        finally:
            conn.close()

    def test_a_label_change_still_works(self, orphaned) -> None:
        pin = pins(orphaned)[0]
        label = orphaned.get("/api/labels").json()["labels"][0]
        response = orphaned.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"label_id": label["id"]}
        )
        assert response.status_code == 200, response.text

    def test_it_heals_the_link_rather_than_duplicating(self, orphaned) -> None:
        pin = pins(orphaned)[0]
        before = _place_events()

        orphaned.patch(
            f"/api/places/{pin['id']}", headers=head(), json={"name": "Healed"}
        )

        assert _place_events() == before, "editing left a second event for one pin"
        conn = db.connect()
        try:
            linked = conn.execute(
                "SELECT event_id FROM places WHERE id = ?", (pin["id"],)
            ).fetchone()["event_id"]
        finally:
            conn.close()
        assert linked is not None, "the pin still does not know its event"


def _place_events() -> int:
    conn = db.connect()
    try:
        return int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM events WHERE source = 'place'"
            ).fetchone()["n"]
        )
    finally:
        conn.close()
