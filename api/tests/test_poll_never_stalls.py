# SPDX-License-Identifier: AGPL-3.0-or-later
"""The status poll has to be cheap, especially while a render is running.

An import on a freshly restored archive sat at 100% and never said it was done.
The render had finished twenty minutes earlier - it is in the history - so what
hung was the screen watching it, waiting on a poll that never came back.

Two things made that possible. Nothing in the browser had a request timeout, so
one unanswered poll stalled the watcher for good. And this endpoint recomputed
the whole job count whenever the count of owed tiles changed, which is every time
a pass finishes: on a large archive that means asking every view which of
thousands of tiles it holds, while the render competes for the same disk.

The browser half is a timeout and a retry. This is the server half: while a
render is going, answer from what is already known.
"""

from __future__ import annotations

import time

from irfaran import db, renderq

from .test_render_queue import (  # noqa: F401 - fixtures used by name
    auth,
    clean,
    client,
    owe_some_work,
    wait_until_idle,
)

#: Generous next to the few milliseconds this should take, and far under
#: anything a person or a watchdog would call a hang.
BUDGET_S = 2.0


def defer_more(clean, tiles) -> None:
    """Incur a debt the running pass cannot know about.

    This is what makes the cache key move while a render is going, and the cache
    key moving is the only time the expensive recompute happens. A test that does
    not do this cannot tell the guard from its absence - the first version of
    these passed either way.
    """
    with db.transaction(clean):
        db.defer_render(clean, tiles)


def wait_for_running(client, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get("/api/render").json()["state"] == "running":
            return
        time.sleep(0.01)
    raise AssertionError("the queue never started")


class TestItStaysCheap:
    def test_a_slow_count_cannot_slow_the_poll(self, client, clean) -> None:
        """The shape of the bug: an expensive answer nobody was waiting for.

        count_jobs is made deliberately slow, because on a real archive it is -
        every view asked which of thousands of owed tiles it holds. If a poll can
        be made to wait on it, the import screen can be made to hang.
        """
        owe_some_work(client, tracks=6)
        client.post("/api/render", headers=auth())
        wait_for_running(client)

        real = renderq.composite.count_jobs

        def slow(*args, **kwargs):
            time.sleep(3.0)
            return real(*args, **kwargs)

        renderq.composite.count_jobs = slow  # type: ignore[assignment]
        try:
            defer_more(clean, {(9100, 9100), (9101, 9101)})

            slowest = 0.0
            polls = 0
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                began = time.monotonic()
                body = client.get("/api/render").json()
                slowest = max(slowest, time.monotonic() - began)
                polls += 1
                if body["state"] not in ("running", "stopping"):
                    break
                time.sleep(0.02)
        finally:
            renderq.composite.count_jobs = real  # type: ignore[assignment]

        assert polls > 3, "the render finished too fast to have polled it"
        assert slowest < BUDGET_S, (
            f"a poll took {slowest:.2f}s while a render was going - it was "
            "waiting on the job count, which is what hung the import screen"
        )
        wait_until_idle(client)

    def test_the_job_count_is_not_worked_out_mid_render(self, client, clean) -> None:
        """The same thing counted rather than timed, so it holds on fast machines."""
        owe_some_work(client, tracks=6)
        client.post("/api/render", headers=auth())
        wait_for_running(client)

        calls = {"n": 0}
        real = renderq.composite.count_jobs

        def counted(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        renderq.composite.count_jobs = counted  # type: ignore[assignment]
        try:
            defer_more(clean, {(9200, 9200), (9201, 9201)})

            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if client.get("/api/render").json()["state"] not in (
                    "running",
                    "stopping",
                ):
                    break
                time.sleep(0.02)
        finally:
            renderq.composite.count_jobs = real  # type: ignore[assignment]

        assert calls["n"] == 0, (
            f"the poll worked out the job count {calls['n']} times during a "
            "render; that is the work that made it slow enough to look hung"
        )
        wait_until_idle(client)


class TestItIsStillCorrectWhenIdle:
    def test_the_count_is_worked_out_once_nothing_is_running(self, client) -> None:
        """Skipping it while busy must not mean never doing it."""
        owe_some_work(client, tracks=3)
        body = client.get("/api/render").json()
        assert body["jobs"] > 0, "an idle queue still has to say what is owed"
        assert body["pending_tiles"] > 0
        assert body["can_start"] is True
