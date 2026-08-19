# SPDX-License-Identifier: AGPL-3.0-or-later
"""The render queue. Server-side, and nobody's browser is part of it.

Rendering used to be driven by an HTTP request that streamed its progress: the
work advanced only while something was pulling on the response, so closing a tab
stopped it mid-pyramid and left the map half drawn with no way back but the
command line. A render is server work. It has no business depending on a window
being open.

So: one worker in the API process, one render at a time, and a state anyone can
read. Starting it is a request that returns immediately; watching it is a
request that returns what the worker is doing. Closing the browser does nothing
at all.

Interruptions are survivable rather than merely tolerated. Each finished job is
written down, so a queue that was stopped - by a button, a restart, a power cut -
resumes without redoing what it already did, and can say exactly how much is
left. The job in flight when the lights go out is the only work repeated.

Two tables between them hold the truth:

  pending_render  which native tiles owe a render, written when data changes
  render_done     which (view, tile) jobs of the current pass are finished

The first survives everything and is cleared only when a pass completes. The
second is the progress bar's memory, cleared at the same moment.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from irfaran import composite, db, history

#: What the worker is doing, for anyone who asks.
IDLE = "idle"
RUNNING = "running"
STOPPING = "stopping"
FAILED = "failed"


@dataclass
class Progress:
    """A snapshot. Cheap to read, because the alternative is not read at all.

    The status endpoint used to recompute the job count from the database on
    every call, which under a running render meant competing with it for the
    same locks - and timing out. Nothing here touches the database.
    """

    state: str = IDLE
    done: int = 0
    total: int = 0
    started_at: str = ""
    finished_at: str = ""
    tiles_written: int = 0
    views: list[str] = field(default_factory=list)
    message: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        elapsed = self.elapsed()
        remaining = None
        if self.state == RUNNING and self.done >= 3 and elapsed and self.total:
            per_job = elapsed / self.done
            remaining = round(per_job * (self.total - self.done))

        return {
            "state": self.state,
            "done": self.done,
            "total": self.total,
            "percent": round(100 * self.done / self.total) if self.total else 0,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed_seconds": round(elapsed, 1) if elapsed else 0,
            "seconds_remaining": remaining,
            "tiles_written": self.tiles_written,
            "views": list(self.views),
            "message": self.message,
            "error": self.error,
        }

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        try:
            began = datetime.fromisoformat(self.started_at)
        except ValueError:
            return 0.0
        end = datetime.now(timezone.utc)
        if self.finished_at:
            try:
                end = datetime.fromisoformat(self.finished_at)
            except ValueError:
                pass
        return max(0.0, (end - began).total_seconds())


class RenderQueue:
    """One render at a time, owned by the server.

    Deliberately a thread rather than a task on the event loop: the render is
    CPU-bound and fans out to a process pool, and the loop has requests to
    answer while it happens.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        #: Whether a worker is still willing to take another pass. Held under
        #: the lock, and the thing start() and the pass loop agree on - see
        #: _claim for why thread liveness is not enough.
        self._looping = False
        self._stop = threading.Event()
        self._progress = Progress()
        self._tiles_root: Path | None = None
        #: Cached answer to "how much work is owed", keyed on how many tiles owe
        #: it. See resume_hint for why this is not computed every time.
        self._owed: tuple[int, dict[str, object]] | None = None

    # -- reading ------------------------------------------------------------

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def snapshot(self) -> dict[str, object]:
        return self._progress.as_dict()

    # -- starting and stopping ---------------------------------------------

    def start(self, tiles_root: Path) -> dict[str, object]:
        """Begin, or say why not. Returns immediately either way."""
        with self._lock:
            if self._looping:
                # A worker is going and will take this on. It has not yet
                # decided to stop - deciding needs this lock - so the work
                # recorded before this call cannot be missed.
                return {"started": False, "reason": "already running", **self.snapshot()}

            conn = db.connect()
            try:
                if not db.pending_render(conn):
                    return {
                        "started": False,
                        "reason": "nothing pending",
                        **self.snapshot(),
                    }
            finally:
                conn.close()

            self._tiles_root = tiles_root
            self._looping = True
            self._stop.clear()
            self._progress = Progress(
                state=RUNNING,
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                message="Working out what needs drawing.",
            )
            self._thread = threading.Thread(
                target=self._run, name="irfaran-render", daemon=True
            )
            self._thread.start()
            return {"started": True, **self.snapshot()}

    def stop(self) -> dict[str, object]:
        """Ask it to stop once the jobs in flight are finished.

        Nothing is discarded: the finished jobs stay written down and the tiles
        still owing stay owing, so resuming carries on rather than starting
        again.
        """
        if not self.running:
            return {"stopping": False, "reason": "not running", **self.snapshot()}

        self._stop.set()
        self._progress.state = STOPPING
        self._progress.message = (
            "Finishing the tiles already being drawn, then stopping."
        )
        return {"stopping": True, **self.snapshot()}

    def resume_hint(self, conn: sqlite3.Connection) -> dict[str, object]:
        """What is owed and how much of it is already done, for the interface.

        Read from the tables rather than from memory, so it is still right after
        a restart when there is no memory to read - "523 tiles still owe a
        render" is the whole answer somebody needs, and it has to survive the
        process that was drawing them.

        The job count is cached against the number of tiles owing, because
        working it out means asking every view which of those tiles it holds and
        expanding each one to its descendants. At 1,646 tiles that took longer
        than the status request was willing to wait - the panel polls this once
        a second while a render runs, and the first version recomputed all of it
        every time. The pending set does not change during a pass, so the count
        cannot either; only `done` moves, and counting one table is cheap.
        """
        pending_count = db.pending_render_count(conn)
        if not pending_count:
            self._owed = None
            return {
                "pending_tiles": 0,
                "jobs": 0,
                "done": 0,
                "remaining": 0,
                "views": [],
            }

        cached = self._owed
        if cached is None or cached[0] != pending_count:
            pending = db.pending_render(conn)
            scoped = db.pending_views(conn)
            if scoped is None:
                views = composite.views_touching(conn, pending)
            else:
                live = set(composite.available_views(conn))
                views = [view for view in scoped if view in live]
            total = composite.count_jobs(
                conn, views, composite.rebuild_scope(pending)
            )
            self._owed = (
                pending_count,
                {"pending_tiles": len(pending), "jobs": total, "views": views},
            )
            cached = self._owed

        done = db.render_done_count(conn)
        fixed = cached[1]
        total = int(fixed["jobs"])  # type: ignore[arg-type]
        return {
            **fixed,
            "done": min(done, total),
            "remaining": max(0, total - done),
        }

    # -- the work -----------------------------------------------------------

    def _run(self) -> None:
        """Pay the debt, and look again before finishing.

        A pass takes what is owing when it starts. Work deferred while it runs -
        a stroke drawn during a render is now the ordinary case rather than an
        odd one - is not in that pass, so this loops until the table is empty
        instead of settling on a snapshot that has gone stale.
        """
        conn = db.connect()
        try:
            root = self._tiles_root
            if root is None:
                self._settle(FAILED, "", error="No tiles directory was given.")
                return

            drawn = 0
            passes = 0
            while not self._stop.is_set():
                pending = self._claim(conn)
                if pending is None:
                    break
                drawn += self._one_pass(conn, root, pending)
                passes += 1

            if self._stop.is_set():
                self._settle(
                    IDLE,
                    f"Stopped with {self._progress.total - self._progress.done} "
                    "of the work left. Resume picks up where this left off.",
                )
                return

            if not passes:
                self._settle(IDLE, "Nothing was pending.")
                return
            self._settle(IDLE, f"Drew {drawn} tiles in {_duration(self._progress.elapsed())}.")
        except Exception as exc:  # noqa: BLE001 - the worker must not die silently
            history.record(
                conn, "error", "render", f"The render stopped: {exc}", {}
            )
            self._settle(FAILED, "", error=str(exc))
        finally:
            with self._lock:
                self._looping = False
            conn.close()

    def _claim(self, conn: sqlite3.Connection) -> set[tuple[int, int]] | None:
        """The tiles owing, or None when there is nothing left to do.

        Reading the debt and giving up on it happen together, under the lock
        that start() takes. Otherwise there is a gap: a worker reads an empty
        table and begins to die, a stroke is recorded, start() sees a thread
        that is still technically alive and declines to begin another - and the
        stroke sits owing a render with nothing running to draw it. Deciding to
        stop while holding the lock means an arriving start() either hands the
        work to this worker or is free to begin its own, never neither.
        """
        with self._lock:
            pending = db.pending_render(conn)
            if pending:
                return pending
            self._looping = False
            return None

    def _one_pass(
        self, conn: sqlite3.Connection, root: Path, pending: set[tuple[int, int]]
    ) -> int:
        """One sweep over the tiles owing. Returns how many tiles it wrote."""
        started = time.monotonic()

        # Which views were owed, if whoever deferred the work knew. Otherwise
        # every view holding data in these tiles, which is wider than the truth
        # and costs a shallow pass per extra view.
        scoped = db.pending_views(conn)
        if scoped is None:
            views = composite.views_touching(conn, pending)
        else:
            live = set(composite.available_views(conn))
            views = [view for view in scoped if view in live]
        kinds = db.pending_kinds(conn)

        root.mkdir(parents=True, exist_ok=True)
        composite.write_placeholders(root, conn)

        already = db.render_done(conn)
        self._progress.views = views
        self._progress.message = f"Drawing {len(views)} views."

        # What earlier passes of this run already accounted for. A run takes as
        # many passes as the work needs - anything deferred while it draws gets
        # another one - and counting each pass from zero sent the bar to 100%,
        # back to nothing, and up again. Two strokes in quick succession were
        # enough to see it.
        base_done = self._progress.done
        base_total = self._progress.total
        base_written = self._progress.tiles_written

        # Jobs already finished in an earlier pass are counted as done
        # rather than hidden, so the bar continues where it left off.
        self._progress.done = base_done + len(already)

        written: dict[str, int] = {}
        for done, total in composite.render_views_iter(
            conn,
            root,
            views,
            scope=composite.rebuild_scope(pending),
            written=written,
            kinds=kinds,
            skip=already,
            on_done=lambda key: db.mark_render_done(conn, key),
            stop=self._stop.is_set,
        ):
            self._progress.done = base_done + len(already) + done
            self._progress.total = base_total + len(already) + total
            self._progress.tiles_written = base_written + sum(written.values())

        if self._stop.is_set():
            return sum(written.values())

        # A complete pass. Both tables are cleared together: the debt is paid
        # and the note of what was done with it is no longer needed. Only the
        # tiles this pass took are cleared - anything deferred while it ran is
        # still owing, and the loop above comes back for it.
        with db.transaction(conn):
            db.clear_pending_render(conn, pending)
            db.clear_render_done(conn)
        self._owed = None

        elapsed = round(time.monotonic() - started, 1)
        tiles = sum(written.values())
        history.record(
            conn,
            "system",
            "render",
            f"Redrew {tiles} tiles across {len(views)} "
            f"{'view' if len(views) == 1 else 'views'} in {_duration(elapsed)}",
            {
                "tiles": tiles,
                "kinds": list(kinds),
                "jobs": self._progress.total,
                "seconds": elapsed,
            },
        )
        return tiles

    def _settle(self, state: str, message: str, error: str = "") -> None:
        self._progress.state = state
        self._progress.message = message
        self._progress.error = error
        self._progress.finished_at = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)}s"
    minutes = seconds / 60
    return f"{minutes:.0f} min" if minutes >= 2 else "just over a minute"


#: One per process. The API imports this rather than making its own.
queue = RenderQueue()
