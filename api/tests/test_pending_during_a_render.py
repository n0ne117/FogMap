# SPDX-License-Identifier: AGPL-3.0-or-later
"""Work that arrives while a render is already running.

A pass reads the tiles owing once, at the start, and builds its job list from
that. Anything deferred afterwards - a stroke drawn while the queue is busy, a
phone reporting a fix, a pin dropped - is not in that list, and the pass used to
delete the whole pending table on its way out. That marked the newcomers paid
without anybody having drawn them: tiles left stale with nothing left to say
they ever owed a render.

It matters more now that drawing defers instead of rendering inline, because
drawing while a render is running stopped being an unusual thing to do and
became the normal one.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from irfaran import db, renderq
from irfaran.main import tiles_root

from . import synthetic
from .synthetic import BASE_LAT, BASE_LON
from .test_render_queue import (  # noqa: F401 - fixtures are used by name
    auth,
    clean,
    client,
    owe_some_work,
    wait_until_idle,
)


def render_entries(client) -> int:
    body = client.get("/api/history?category=system&limit=50").json()
    return sum(1 for entry in body["entries"] if entry["action"] == "render")


def wait_for_running(client, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/render").json()["state"] == "running":
            return
        time.sleep(0.01)
    raise AssertionError("the queue never started")


class TestDebtIncurredMidPass:
    def test_a_late_arrival_is_drawn_rather_than_marked_paid(self, client, clean) -> None:
        """Ground imported mid-render must end up on disk, not merely settled."""
        owe_some_work(client, tracks=3)
        client.post("/api/render", headers=auth())
        wait_for_running(client)

        # Somewhere the running pass certainly does not cover. The debt owed
        # before this import is subtracted, because the running pass has not
        # cleared its own snapshot yet and those tiles are not what is on trial.
        owed_before = db.pending_render(clean)
        far = [(BASE_LON + 4.0 + n * 0.0002, BASE_LAT + 2.0) for n in range(40)]
        client.post(
            "/api/ingest/gpx?defer_render=true",
            headers=auth(),
            files={
                "file": (
                    "late.gpx",
                    # Its own clock. Every synthetic document defaults to the
                    # same start time, and ingest is idempotent on it, so a
                    # second one would be skipped as already imported and the
                    # test would prove nothing while appearing to pass.
                    synthetic.gpx_document(
                        far,
                        name="late",
                        start=datetime(2025, 6, 1, 9, 0, tzinfo=timezone.utc),
                    ),
                    "application/gpx+xml",
                ),
            },
        )
        late_tiles = db.pending_render(clean) - owed_before
        assert late_tiles, "the late import owed nothing new, so this proves nothing"

        final = wait_until_idle(client)
        assert final["pending_tiles"] == 0

        # The debt is settled *and* the ground is drawn. Before the fix the
        # first half was true and the second was not.
        root = tiles_root()
        drawn = {
            (int(path.parent.name), int(path.stem))
            for path in root.rglob("dark/all/fog/14/*/*.png")
        }
        missed = late_tiles - drawn
        assert not missed, (
            "tiles deferred while a render ran were cleared without being "
            f"drawn: {sorted(missed)[:8]}"
        )

    def test_it_comes_back_for_it_in_a_second_pass(self, client, clean) -> None:
        """The loop, seen from outside: two passes, because two debts existed."""
        before = render_entries(client)
        owe_some_work(client, tracks=3)
        client.post("/api/render", headers=auth())
        wait_for_running(client)

        with db.transaction(clean):
            db.defer_render(clean, {(9000, 9000)})

        wait_until_idle(client)
        assert render_entries(client) >= before + 2


class TestHandingOver:
    """Whether a worker is taking more work, and who decides.

    These reach for a private flag deliberately. The condition being pinned is
    not visible from outside - a thread that has read an empty table and is on
    its way out still looks alive - and that gap is precisely where a stroke
    could be left owing a render with nothing running to draw it.
    """

    def test_a_finished_worker_stops_claiming(self, client) -> None:
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        wait_until_idle(client)

        assert renderq.queue._looping is False, (
            "the worker still claims to be taking passes after going idle, so "
            "the next stroke would be handed to a thread that has stopped"
        )

    def test_a_working_worker_is_left_to_it(self, client) -> None:
        """A second start does not begin a second render - it defers to the first."""
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        wait_for_running(client)

        again = client.post("/api/render", headers=auth()).json()
        assert again["started"] is False
        assert again["reason"] == "already running"
        wait_until_idle(client)
