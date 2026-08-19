# SPDX-License-Identifier: AGPL-3.0-or-later
"""The render queue, which belongs to the server.

Rendering used to be driven by a streaming HTTP response: the work advanced only
while a browser held that response open, so closing a tab stopped it mid-pyramid
and left the map half drawn with nothing in the interface offering a way back.

What these check is the part that makes that impossible now - a render that can
be stopped and resumed without repeating itself, and a state anyone can read
without touching the render's own locks.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from irfaran import composite, db, renderq
from irfaran.main import app, tiles_root

from . import synthetic
from .synthetic import BASE_LAT, BASE_LON

TOKEN = "synthetic-queue-token"


@pytest.fixture
def clean():
    conn = db.open_initialised()
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM pending_render")
    conn.execute("DELETE FROM render_done")
    conn.execute("DELETE FROM log")
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def client(monkeypatch, clean):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict[str, str]:
    return {"X-Irfaran-Token": TOKEN}


def owe_some_work(client, tracks: int = 3) -> int:
    """Import a few tracks with the render deferred, and return the tiles owed."""
    for index in range(tracks):
        points = [
            (BASE_LON + index * 0.05 + n * 0.0002, BASE_LAT + index * 0.01)
            for n in range(40)
        ]
        client.post(
            "/api/ingest/gpx?defer_render=true",
            headers=auth(),
            files={
                "file": (
                    f"q{index}.gpx",
                    synthetic.gpx_document(points, name=f"q{index}"),
                    "application/gpx+xml",
                )
            },
        )
    return int(client.get("/api/render").json()["pending_tiles"])


def wait_until_idle(client, timeout: float = 120.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = client.get("/api/render").json()
        if state["state"] not in ("running", "stopping"):
            return state
        time.sleep(0.03)
    raise AssertionError("the queue never went idle")


class TestState:
    def test_idle_with_nothing_owed(self, client) -> None:
        body = client.get("/api/render").json()
        assert body["state"] == "idle"
        assert body["pending_tiles"] == 0
        assert body["can_start"] is False
        assert body["can_stop"] is False

    def test_owed_work_offers_a_start(self, client) -> None:
        assert owe_some_work(client) > 0
        body = client.get("/api/render").json()
        assert body["can_start"] is True
        assert body["jobs"] > 0
        assert body["jobs_remaining"] == body["jobs"]
        assert body["pending_views"]

    def test_status_needs_no_token(self, client) -> None:
        assert client.get("/api/render").status_code == 200

    def test_starting_needs_the_token(self, client) -> None:
        assert client.post("/api/render").status_code in (401, 403)


class TestARun:
    def test_it_finishes_and_settles_the_debt(self, client) -> None:
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        final = wait_until_idle(client)

        assert final["state"] == "idle"
        assert final["pending_tiles"] == 0
        assert final["tiles_written"] > 0
        assert final["done"] == final["total"]

    def test_the_progress_note_is_cleared_when_it_completes(self, client, clean) -> None:
        """render_done is the resume memory. A finished pass has no use for it."""
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        wait_until_idle(client)
        assert db.render_done_count(clean) == 0

    def test_it_is_recorded_in_the_history(self, client) -> None:
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        wait_until_idle(client)

        entries = client.get("/api/history?category=system").json()["entries"]
        renders = [e for e in entries if e["action"] == "render"]
        assert renders, "a completed render left no trace"
        assert "jobs" in (renders[0]["detail"] or {})
        assert "seconds" in (renders[0]["detail"] or {})


class TestStoppingAndResuming:
    def test_stopping_keeps_what_was_done(self, client, clean) -> None:
        """The whole point: an interrupted render resumes rather than restarts."""
        owe_some_work(client, tracks=6)
        total = client.get("/api/render").json()["jobs"]
        assert total > 4, "not enough work to interrupt meaningfully"

        client.post("/api/render", headers=auth())

        # Stop as soon as it has actually done something.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if db.render_done_count(clean) > 0:
                break
            time.sleep(0.01)
        client.post("/api/render/stop", headers=auth())
        stopped = wait_until_idle(client)

        done_after_stop = db.render_done_count(clean)
        assert done_after_stop > 0, "nothing was written down as finished"
        assert stopped["pending_tiles"] > 0, "the debt was cleared by a stop"

        # Resuming reports the earlier work as already done.
        state = client.get("/api/render").json()
        assert state["jobs_done"] == done_after_stop
        assert state["jobs_remaining"] == max(0, state["jobs"] - done_after_stop)
        assert state["can_start"] is True

    def test_resuming_finishes_the_job(self, client, clean) -> None:
        owe_some_work(client, tracks=6)
        client.post("/api/render", headers=auth())
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and db.render_done_count(clean) == 0:
            time.sleep(0.01)
        client.post("/api/render/stop", headers=auth())
        wait_until_idle(client)

        client.post("/api/render", headers=auth())
        final = wait_until_idle(client)
        assert final["pending_tiles"] == 0
        assert db.render_done_count(clean) == 0

    def test_stopping_when_nothing_runs_says_so(self, client) -> None:
        body = client.post("/api/render/stop", headers=auth()).json()
        assert body["stopping"] is False
        assert body["reason"] == "not running"


class TestSurvivingARestart:
    def test_the_debt_and_the_progress_outlive_the_process(self, client, clean) -> None:
        """Both live in the database, which is what makes tomorrow's resume work.

        A restart loses the worker and its in-memory progress; it must not lose
        the knowledge of what is owed or of what was already drawn.
        """
        owe_some_work(client, tracks=4)
        db.mark_render_done(clean, ("all", 8214, 8180))

        # A fresh queue object stands in for a restarted process.
        fresh = renderq.RenderQueue()
        hint = fresh.resume_hint(clean)

        assert hint["pending_tiles"] > 0
        assert hint["done"] == 1
        assert hint["remaining"] == hint["jobs"] - 1
        assert fresh.snapshot()["state"] == "idle"


class TestSkippingFinishedJobs:
    def test_render_views_iter_skips_what_is_already_done(self, clean) -> None:
        """The mechanism underneath a resume."""
        points = [(BASE_LON + n * 0.0002, BASE_LAT) for n in range(30)]
        from irfaran.ingest import common, gpx

        common.ingest_tracks(clean, "workout", gpx.parse(synthetic.gpx_document(points)))
        root = tiles_root()
        composite.write_placeholders(root, clean)

        views = ["all"]
        every = list(composite.render_views_iter(clean, root, views, workers=1))
        total = every[-1][1]
        assert total >= 2

        seen: list[tuple[str, int, int]] = []
        list(
            composite.render_views_iter(
                clean, root, views, workers=1, on_done=seen.append
            )
        )
        assert len(seen) == total

        # Hand back one finished job and the queue is one shorter.
        again = list(
            composite.render_views_iter(
                clean, root, views, workers=1, skip={seen[0]}
            )
        )
        assert again[-1][1] == total - 1

    def test_stop_ends_it_early(self, clean) -> None:
        points = [(BASE_LON + n * 0.0002, BASE_LAT) for n in range(30)]
        from irfaran.ingest import common, gpx

        common.ingest_tracks(clean, "workout", gpx.parse(synthetic.gpx_document(points)))
        root = tiles_root()
        composite.write_placeholders(root, clean)

        steps = list(
            composite.render_views_iter(
                clean, root, ["all"], workers=1, stop=lambda: True
            )
        )
        # One job runs before the first check, and then it gives up.
        assert steps[-1][0] < steps[-1][1] or steps[-1][1] <= 1


class TestOneShape:
    """Start, stop and status all answer with the same fields.

    Start and stop used to reply with the worker's snapshot alone, which knows
    nothing about what is owed. The panel painted that reply, read `undefined`
    for pending_tiles, called toLocaleString on it, and put a TypeError on the
    page. One builder now serves all three.
    """

    #: Everything the interface reads. A reply missing any of these is a bug.
    REQUIRED = (
        "state",
        "done",
        "total",
        "percent",
        "tiles_written",
        "pending_tiles",
        "jobs",
        "jobs_done",
        "jobs_remaining",
        "pending_views",
        "rendering_views",
        "workers",
        "can_start",
        "can_stop",
        "message",
        "error",
        "seconds_per_job",
        "estimated_seconds",
    )

    def test_status_carries_every_field(self, client) -> None:
        body = client.get("/api/render").json()
        assert not [key for key in self.REQUIRED if key not in body]

    def test_starting_carries_every_field(self, client) -> None:
        owe_some_work(client)
        body = client.post("/api/render", headers=auth()).json()
        missing = [key for key in self.REQUIRED if key not in body]
        assert not missing, f"start's reply is missing {missing}"
        assert body["started"] is True
        wait_until_idle(client)

    def test_a_refused_start_carries_every_field(self, client) -> None:
        body = client.post("/api/render", headers=auth()).json()
        missing = [key for key in self.REQUIRED if key not in body]
        assert not missing, f"a refused start is missing {missing}"
        assert body["started"] is False
        assert body["reason"] == "nothing pending"

    def test_stopping_carries_every_field(self, client) -> None:
        owe_some_work(client)
        client.post("/api/render", headers=auth())
        body = client.post("/api/render/stop", headers=auth()).json()
        missing = [key for key in self.REQUIRED if key not in body]
        assert not missing, f"stop's reply is missing {missing}"
        wait_until_idle(client)

    def test_a_refused_stop_carries_every_field(self, client) -> None:
        body = client.post("/api/render/stop", headers=auth()).json()
        missing = [key for key in self.REQUIRED if key not in body]
        assert not missing, f"a refused stop is missing {missing}"
        assert body["stopping"] is False

    def test_no_number_the_interface_formats_is_ever_null(self, client) -> None:
        """toLocaleString on null is the exact crash this replaces."""
        counted = (
            "done",
            "total",
            "percent",
            "tiles_written",
            "pending_tiles",
            "jobs",
            "jobs_done",
            "jobs_remaining",
            "workers",
        )
        owe_some_work(client)
        for reply in (
            client.get("/api/render").json(),
            client.post("/api/render", headers=auth()).json(),
            client.post("/api/render/stop", headers=auth()).json(),
        ):
            for key in counted:
                assert isinstance(reply.get(key), int), f"{key} is {reply.get(key)!r}"
        wait_until_idle(client)


class TestAResumeKeepsWhatItSkipped:
    """A resume must not delete the work it is resuming from.

    Pruning removes tiles inside the scope that a pass did not write, which is
    how ground whose data has gone stops being drawn. On a resumed pass the
    skipped jobs' tiles are absent from that accounting - so pruning deleted
    them, and the resume destroyed the very work it existed to preserve.

    Found on a real archive: a resume removed about 1,300 deep tiles from the
    cumulative view, which is rendered first and so was almost entirely skipped.
    The symptom was tracks vanishing when zoomed in, and the giveaway was `all`
    holding fewer tiles than a single year view - impossible, since it is the
    union of all of them.
    """

    def a_track(self, conn) -> None:
        from irfaran.ingest import common, gpx

        points = [(BASE_LON + n * 0.0002, BASE_LAT) for n in range(60)]
        common.ingest_tracks(
            conn, "workout", gpx.parse(synthetic.gpx_document(points))
        )

    def test_skipped_tiles_survive(self, clean) -> None:
        """Some jobs run and some are skipped, which is what a resume is.

        Skipping every job leaves nothing in the accounting and pruning is
        skipped along with it - so a test that skips everything passes whether
        the bug is present or not. The bug needs one job to run: that fills the
        accounting, pruning goes ahead, and everything the run did not touch is
        deleted.
        """
        self.a_track(clean)
        root = tiles_root()
        composite.write_placeholders(root, clean)

        native = composite.tiles_with_data(clean, composite.view_layers("all"))
        assert native, "the track produced no native tiles"
        scope = composite.rebuild_scope(native)

        keys: list[tuple[str, int, int]] = []
        list(
            composite.render_views_iter(
                clean, root, ["all"], workers=1, scope=scope, on_done=keys.append
            )
        )
        before = {path for path in root.rglob("dark/all/**/*.png")}
        assert len(before) > 4, "the first pass drew too little to be a fair test"
        assert len(keys) >= 2, "need more than one job to skip some and run some"

        # A resume: everything but the last job is already done.
        list(
            composite.render_views_iter(
                clean,
                root,
                ["all"],
                workers=1,
                scope=scope,
                skip=set(keys[:-1]),
            )
        )
        after = {path for path in root.rglob("dark/all/**/*.png")}

        deleted = before - after
        assert not deleted, (
            f"a resume deleted {len(deleted)} tiles belonging to jobs it "
            "skipped, which is the work it existed to preserve"
        )

    def test_a_full_pass_still_prunes(self, clean) -> None:
        """The safety must not have been bought by never pruning again."""
        self.a_track(clean)
        root = tiles_root()
        composite.write_placeholders(root, clean)

        list(composite.render_views_iter(clean, root, ["all"], workers=1))

        # A tile inside the scope that no data accounts for.
        from irfaran import geo

        x, y = geo.lonlat_to_tile(BASE_LON, BASE_LAT)
        stray = composite.tile_path(root, "dark", "all", "fog", 16, x * 4, y * 4)
        stray.parent.mkdir(parents=True, exist_ok=True)
        stray.write_bytes(b"not a real tile")
        assert stray.is_file()

        scope = composite.rebuild_scope({(x, y)})
        list(
            composite.render_views_iter(
                clean, root, ["all"], workers=1, scope=scope
            )
        )
        assert not stray.is_file(), "a complete pass no longer prunes stale tiles"

    def test_a_resume_through_the_queue_keeps_its_tiles(self, client, clean) -> None:
        """The same guarantee, end to end through the queue rather than the loop.

        The invariant that exposed this on a real archive was that the
        cumulative view held fewer tiles than a single year view, which cannot
        be true - it is the union of all of them. That comparison is not worth
        asserting here, because the suite shares one tiles directory and every
        earlier test leaves its own tiles in it. What is worth asserting is that
        a stop and a resume through the queue lose nothing.
        """
        import time

        owe_some_work(client, tracks=5)
        client.post("/api/render", headers=auth())

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and db.render_done_count(clean) < 2:
            time.sleep(0.01)
        client.post("/api/render/stop", headers=auth())
        wait_until_idle(client)

        root = tiles_root()
        after_stop = {path for path in root.rglob("dark/all/**/*.png")}
        assert after_stop, "the first stretch drew nothing"

        client.post("/api/render", headers=auth())
        wait_until_idle(client)

        after_resume = {path for path in root.rglob("dark/all/**/*.png")}
        lost = after_stop - after_resume
        assert not lost, f"the resume lost {len(lost)} tiles the first stretch drew"
