# SPDX-License-Identifier: AGPL-3.0-or-later
"""The history log behind the History tab.

Two things it must never do: grow without bound in a database somebody backs
up, and let a tracker that delivers every few minutes push out everything worth
reading. And one thing it must never break: whatever it is recording.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from irfaran import db, history
from irfaran.main import app

TOKEN = "synthetic-history-token"


@pytest.fixture
def conn():
    connection = db.open_initialised()
    connection.execute("DELETE FROM log")
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


class TestRecording:
    def test_an_entry_comes_back(self, conn) -> None:
        history.record(conn, "manual", "import", "Imported a file")
        entries = history.recent(conn)
        assert len(entries) == 1
        assert entries[0]["category"] == "manual"
        assert entries[0]["message"] == "Imported a file"
        assert entries[0]["count"] == 1

    def test_newest_first(self, conn) -> None:
        for index in range(3):
            history.record(conn, "system", "render", f"render {index}")
        assert [e["message"] for e in history.recent(conn)] == [
            "render 2", "render 1", "render 0"
        ]

    def test_every_category_is_accepted(self, conn) -> None:
        for category in history.CATEGORIES:
            history.record(conn, category, "x", "y")
        assert set(history.counts(conn)) == set(history.CATEGORIES)
        assert all(count == 1 for count in history.counts(conn).values())

    def test_an_unknown_category_is_a_programming_error(self, conn) -> None:
        with pytest.raises(history.HistoryError):
            history.record(conn, "warning", "x", "y")

    def test_detail_survives_the_round_trip(self, conn) -> None:
        history.record(conn, "manual", "draw", "Drew", {"tiles": 7})
        assert history.recent(conn)[0]["detail"] == {"tiles": 7}

    def test_recording_cannot_break_what_it_records(self, conn) -> None:
        """A history line is the least important writer here.

        An import that failed because writing a log line failed would be a
        worse bug than the missing line.
        """
        conn.execute("DROP TABLE log")
        history.record(conn, "manual", "import", "still fine")  # must not raise


class TestCoalescing:
    def test_a_repeat_folds_into_the_previous_line(self, conn) -> None:
        for _ in range(4):
            history.record(conn, "source", "live:overland", "1 fix", coalesce=True)

        entries = history.recent(conn)
        assert len(entries) == 1, "a phone posting all day must not be 300 lines"
        assert entries[0]["count"] == 4

    def test_a_different_action_starts_its_own_line(self, conn) -> None:
        history.record(conn, "source", "live:overland", "a", coalesce=True)
        history.record(conn, "source", "live:owntracks", "b", coalesce=True)
        assert len(history.recent(conn)) == 2

    def test_something_else_in_between_starts_a_new_line(self, conn) -> None:
        """Folding into a line that is no longer the newest would reorder time."""
        history.record(conn, "source", "live:overland", "a", coalesce=True)
        history.record(conn, "manual", "draw", "drew")
        history.record(conn, "source", "live:overland", "b", coalesce=True)
        assert [e["category"] for e in history.recent(conn)] == [
            "source", "manual", "source"
        ]

    def test_an_old_line_is_not_folded_into(self, conn) -> None:
        history.record(conn, "source", "live:overland", "a", coalesce=True)
        stale = (
            datetime.now(timezone.utc)
            - timedelta(minutes=history.COALESCE_MINUTES + 5)
        ).isoformat(timespec="seconds")
        conn.execute("UPDATE log SET at = ?", (stale,))

        history.record(conn, "source", "live:overland", "b", coalesce=True)
        assert len(history.recent(conn)) == 2

    def test_without_coalescing_every_call_is_its_own_line(self, conn) -> None:
        for _ in range(3):
            history.record(conn, "manual", "draw", "Drew a route")
        assert len(history.recent(conn)) == 3


class TestRetention:
    def test_it_is_capped_by_count(self, conn, monkeypatch) -> None:
        monkeypatch.setattr(history, "MAX_ENTRIES", 10)
        for index in range(25):
            history.record(conn, "system", "render", f"r{index}")

        entries = history.recent(conn)
        assert len(entries) == 10
        assert entries[0]["message"] == "r24", "it kept the wrong end"

    def test_it_is_capped_by_age(self, conn) -> None:
        history.record(conn, "system", "render", "ancient")
        old = (
            datetime.now(timezone.utc) - timedelta(days=history.MAX_AGE_DAYS + 1)
        ).isoformat(timespec="seconds")
        conn.execute("UPDATE log SET at = ?", (old,))

        history.record(conn, "system", "render", "fresh")
        assert [e["message"] for e in history.recent(conn)] == ["fresh"]

    def test_clearing_forgets_everything(self, conn) -> None:
        for index in range(5):
            history.record(conn, "manual", "draw", f"d{index}")
        assert history.clear(conn) == 5
        assert history.recent(conn) == []


class TestEndpoint:
    def test_it_is_readable_without_a_token(self, client, conn) -> None:
        history.record(conn, "manual", "import", "Imported")
        response = client.get("/api/history")
        assert response.status_code == 200
        assert response.json()["entries"][0]["message"] == "Imported"

    def test_it_reports_what_it_keeps(self, client) -> None:
        kept = client.get("/api/history").json()["kept"]
        assert kept["entries"] == history.MAX_ENTRIES
        assert kept["days"] == history.MAX_AGE_DAYS

    def test_filtering_by_category(self, client, conn) -> None:
        history.record(conn, "error", "import", "broke")
        history.record(conn, "manual", "draw", "drew")
        entries = client.get("/api/history?category=error").json()["entries"]
        assert [e["category"] for e in entries] == ["error"]

    def test_a_bad_category_is_refused(self, client) -> None:
        assert client.get("/api/history?category=nonsense").status_code == 400

    def test_clearing_needs_the_token(self, client) -> None:
        assert client.delete("/api/history").status_code in (401, 403)

    def test_clearing_works_with_it(self, client, conn) -> None:
        history.record(conn, "manual", "draw", "drew")
        assert client.delete("/api/history", headers=auth()).status_code == 200
        assert client.get("/api/history").json()["entries"] == []


class TestWhatGetsRecorded:
    def test_a_drawn_stroke_is_recorded_as_manual(self, client) -> None:
        client.post(
            "/api/events",
            headers=auth(),
            json={
                "source": "manual",
                "op": "add",
                "radius_m": 20,
                "geometry": {"type": "Point", "coordinates": [9.9, 44.4]},
            },
        )
        entries = client.get("/api/history?category=manual").json()["entries"]
        assert entries, "drawing recorded nothing"
        assert "Drew" in entries[0]["message"]

    def test_a_failed_import_is_recorded_as_an_error(self, client) -> None:
        client.post(
            "/api/ingest/gpx",
            headers=auth(),
            files={"file": ("broken.gpx", b"this is not gpx", "application/gpx+xml")},
        )
        entries = client.get("/api/history?category=error").json()["entries"]
        assert entries, "a failed import recorded nothing"
        assert "broken.gpx" in entries[0]["message"]

    def test_a_settings_value_is_never_written_down(self, client) -> None:
        """A setting's value can be a token, and history travels with a backup."""
        client.patch(
            "/api/settings", headers=auth(), json={"api_token": "super-secret-value"}
        )
        text = client.get("/api/history").text
        assert "super-secret-value" not in text
        assert "api_token" in text, "the name is worth keeping, only the value is not"
