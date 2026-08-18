# SPDX-License-Identifier: AGPL-3.0-or-later
"""Saying how long a render will take, before starting it.

Four long-distance tracks measured twelve minutes of work on a real archive,
because every z14 tile a track crosses also has its z15 and z16 descendants
stamped for each theme, each kind and each view containing it. Four walks round
a town are twelve seconds. A wait nobody warned you about is indistinguishable
from a hang, and the size is knowable before any work starts.

The rate is learned from this machine's own past renders. Self-hosted hardware
is whatever somebody had, so a number measured here beats a constant measured
somewhere else - and until there is one, the answer is "no idea" rather than a
guess.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import composite, db, history
from irfaran.main import _seconds_per_job, app

TOKEN = "synthetic-estimate-token"


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


def a_render(conn, jobs: int, seconds: float) -> None:
    history.record(
        conn, "system", "render", f"Redrew tiles in {seconds}s",
        {"tiles": 100, "jobs": jobs, "seconds": seconds},
    )
    conn.commit()


class TestTheRate:
    def test_there_is_none_until_a_render_has_happened(self, conn) -> None:
        assert _seconds_per_job(conn) is None

    def test_it_is_learned_from_one_render(self, conn) -> None:
        a_render(conn, jobs=100, seconds=50)
        assert _seconds_per_job(conn) == pytest.approx(0.5)

    def test_it_averages_over_several(self, conn) -> None:
        a_render(conn, jobs=100, seconds=50)
        a_render(conn, jobs=100, seconds=150)
        # Weighted by work, not a mean of rates: 200 jobs took 200 seconds.
        assert _seconds_per_job(conn) == pytest.approx(1.0)

    def test_nonsense_is_ignored_rather_than_averaged_in(self, conn) -> None:
        a_render(conn, jobs=100, seconds=50)
        history.record(conn, "system", "render", "odd", {"jobs": 0, "seconds": 0})
        history.record(conn, "system", "render", "odder", {"jobs": "many"})
        conn.commit()
        assert _seconds_per_job(conn) == pytest.approx(0.5)

    def test_other_history_does_not_count(self, conn) -> None:
        history.record(conn, "manual", "import", "a file", {"jobs": 9, "seconds": 9})
        conn.commit()
        assert _seconds_per_job(conn) is None


class TestTheEndpoint:
    def test_nothing_pending_costs_nothing(self, client) -> None:
        body = client.get("/api/render").json()
        assert body["pending_tiles"] == 0
        assert body["jobs"] == 0
        assert body["estimated_seconds"] is None

    def test_it_reports_an_estimate_once_a_rate_is_known(self, client, conn) -> None:
        a_render(conn, jobs=100, seconds=200)
        with db.transaction(conn):
            db.defer_render(conn, {(8214, 8180)})

        body = client.get("/api/render").json()
        assert body["pending_tiles"] == 1
        assert body["seconds_per_job"] == pytest.approx(2.0)

        # The tile may belong to no view, in which case there is no work to
        # estimate and saying so beats saying zero seconds. What must hold is
        # that the estimate and the remaining work agree.
        if body["jobs_remaining"]:
            assert body["estimated_seconds"] == round(body["jobs_remaining"] * 2.0)
        else:
            assert body["estimated_seconds"] is None

    def test_the_estimate_needs_no_token(self, client) -> None:
        assert client.get("/api/render").status_code == 200


class TestCountingJobs:
    def test_no_views_is_no_work(self, conn) -> None:
        assert composite.count_jobs(conn, []) == 0

    def test_it_matches_what_the_queue_actually_does(self, client, conn) -> None:
        """The estimate is worthless if it disagrees with the real queue."""
        import time

        client.post(
            "/api/events",
            headers={"X-Irfaran-Token": TOKEN},
            json={
                "source": "manual",
                "op": "add",
                "radius_m": 30,
                "geometry": {"type": "Point", "coordinates": [10.5, 45.5]},
            },
        )
        with db.transaction(conn):
            db.defer_render(conn, {(8214, 8180)})

        predicted = client.get("/api/render").json()["jobs"]
        client.post("/api/render", headers={"X-Irfaran-Token": TOKEN})

        deadline = time.monotonic() + 60
        final = None
        while time.monotonic() < deadline:
            final = client.get("/api/render").json()
            if final["state"] not in ("running", "stopping"):
                break
            time.sleep(0.05)

        assert final is not None
        assert predicted == final["total"], (
            "the estimate and the queue disagree about how much work there is"
        )
