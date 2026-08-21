# SPDX-License-Identifier: AGPL-3.0-or-later
"""Place names and points of interest, extracted from the basemap.

The archive cannot be searched as it stands - it holds rendered tiles, so a name
exists in it as something to draw at a zoom rather than as an index. This walks
it once and writes the names into SQLite, where they can be searched offline like
everything else here. Nothing leaves the machine, which is the whole reason this
exists rather than a call to a geocoding service.

Two builds, not one, because they cost different orders of magnitude. Measured on
the installed 137 GB archive:

    places   z10   478,382 tiles     2.6 min    1,066,806 distinct names
    pois     z15   ~118M tiles       hours      millions

The depths are not arbitrary. A label carries a `min_zoom` saying where it starts
being drawn, and it appears in every tile below that - but the field is advisory:
a z15 tile carries POIs marked `min_zoom: 16`, which is the only reason a
restaurant is reachable at all when the archive stops at z15. Settlements are all
present by z10. So places are cheap and POIs are an overnight job.

Three things learned by measuring, all designed in here:

Labels are buffered into neighbouring tiles, so 55% of what a scan reads is a
repeat - `Ai tre tini` appears four times around Caorle. Duplicates are dropped
on the way in where they are close together in the scan, and collapsed again at
query time for the ones that are not.

A build must never be the reason an edit waits. It yields to the render queue
rather than competing with it for the cores, and its progress is written down so
stopping and resuming costs a batch rather than the whole scan.

And a build writes a new generation rather than replacing the live one, so the
existing names keep answering until the new set is complete - and a build that
fails or is stopped leaves a working index rather than a hole.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from irfaran import db, history, mvt, pmtiles, renderq

#: The two things that can be built, the layer each comes from, and how deep the
#: scan has to go to find all of it.
KINDS = {
    "place": {"layer": "places", "zoom": 10, "label": "Place names"},
    "poi": {"layer": "pois", "zoom": 15, "label": "Points of interest"},
}

IDLE = "idle"
BUILDING = "building"
STOPPING = "stopping"
FAILED = "failed"

#: Rows held before writing. Big enough that the write is not most of the work,
#: small enough that stopping loses a second rather than a minute.
BATCH = 2000

#: How many recent (name, position) keys to remember while scanning.
#:
#: Buffered duplicates sit in neighbouring tiles, and neighbouring tiles are
#: close together in Hilbert order, so a bounded memory catches almost all of
#: them for a few tens of megabytes rather than holding every key of a
#: fifteen-million-row scan. Whatever slips past is collapsed at query time.
RECENT_KEYS = 400_000

SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS gazetteer USING fts5(
  name,
  kind UNINDEXED,
  category UNINDEXED,
  lat UNINDEXED,
  lon UNINDEXED,
  generation UNINDEXED,
  tokenize = "unicode61 remove_diacritics 2"
);

-- One row per kind: what is being built, how far it got, and where it came from.
--
-- `cursor` is the last tile id finished. Entries come out of the archive in
-- ascending id order, so a resume is a matter of starting after it rather than
-- remembering which tiles were done.
CREATE TABLE IF NOT EXISTS gazetteer_build (
  kind        TEXT PRIMARY KEY,
  generation  INTEGER NOT NULL,
  zoom        INTEGER NOT NULL,
  cursor      INTEGER NOT NULL DEFAULT -1,
  tiles_done  INTEGER NOT NULL DEFAULT 0,
  tiles_total INTEGER NOT NULL DEFAULT 0,
  rows_written INTEGER NOT NULL DEFAULT 0,
  duplicates  INTEGER NOT NULL DEFAULT 0,
  archive     TEXT,
  state       TEXT NOT NULL DEFAULT 'idle',
  started_at  TEXT,
  finished_at TEXT,
  error       TEXT
);
"""


def install(conn: sqlite3.Connection) -> None:
    """Create the tables. Safe on every startup, like the rest of the schema."""
    conn.executescript(SCHEMA)


def live_generation(conn: sqlite3.Connection, kind: str) -> int:
    """Which generation of a kind is the one being searched. 0 means none yet."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (f"gazetteer_live_{kind}",)
    ).fetchone()
    try:
        return int(str(row["value"])) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_live(conn: sqlite3.Connection, kind: str, generation: int) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"gazetteer_live_{kind}", str(generation)),
    )


def provenance(path: Path) -> str:
    """Which archive an index was built from, so staleness is visible.

    A gazetteer outlives the basemap it came from unless somebody can tell they
    have diverged - and then it quietly sends people to places that have moved.
    """
    stat = path.stat()
    return f"{path.name} {stat.st_size} {int(stat.st_mtime)}"


def status(conn: sqlite3.Connection, archive_path: Path | None = None) -> dict[str, object]:
    """What is built, what is being built, and whether it is stale."""
    rows = {
        str(row["kind"]): row
        for row in conn.execute("SELECT * FROM gazetteer_build")
    }
    current = provenance(archive_path) if archive_path and archive_path.is_file() else ""

    out: dict[str, object] = {"archive": current, "kinds": {}}
    for kind, about in KINDS.items():
        row = rows.get(kind)
        live = live_generation(conn, kind)

        # What the last completed build wrote, rather than a COUNT over the
        # table. The interface polls this while a build runs, and counting
        # fifteen million rows of points of interest on every poll is how a
        # status endpoint becomes the slowest thing in the application.
        counted = int(row["rows_written"]) if row and live else 0

        out["kinds"][kind] = {  # type: ignore[index]
            "label": about["label"],
            "zoom": about["zoom"],
            "built": bool(live),
            "names": counted,
            "state": str(row["state"]) if row else IDLE,
            "tiles_done": int(row["tiles_done"]) if row else 0,
            "tiles_total": int(row["tiles_total"]) if row else 0,
            "percent": (
                round(100 * int(row["tiles_done"]) / int(row["tiles_total"]))
                if row and int(row["tiles_total"])
                else 0
            ),
            "rows_written": int(row["rows_written"]) if row else 0,
            "duplicates": int(row["duplicates"]) if row else 0,
            "built_from": str(row["archive"] or "") if row else "",
            "stale": bool(row and live and current and str(row["archive"] or "") != current),
            "started_at": str(row["started_at"] or "") if row else "",
            "finished_at": str(row["finished_at"] or "") if row else "",
            "error": str(row["error"] or "") if row else "",
        }
    return out


def remove(conn: sqlite3.Connection, kind: str) -> int:
    """Throw away an extracted index. Returns how many rows went.

    Every generation, not only the live one: a stopped build leaves a partial
    generation behind, and "delete" should mean the disk is back.
    """
    if kind not in KINDS:
        raise KeyError(kind)

    with db.transaction(conn):
        removed = conn.execute(
            "SELECT COUNT(*) AS n FROM gazetteer WHERE kind = ?", (kind,)
        ).fetchone()["n"]
        conn.execute("DELETE FROM gazetteer WHERE kind = ?", (kind,))
        conn.execute("DELETE FROM gazetteer_build WHERE kind = ?", (kind,))
        conn.execute(
            "DELETE FROM settings WHERE key = ?", (f"gazetteer_live_{kind}",)
        )
        # Switching the search off too: leaving it on with nothing behind it
        # means a search that silently finds nothing.
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, 'false') "
            "ON CONFLICT(key) DO UPDATE SET value = 'false'",
            ("search_place_names" if kind == "place" else "search_pois",),
        )
    return int(removed)


@dataclass
class Progress:
    kind: str = ""
    state: str = IDLE
    tiles_done: int = 0
    tiles_total: int = 0
    rows: int = 0
    duplicates: int = 0
    yielded: bool = False
    message: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "state": self.state,
            "tiles_done": self.tiles_done,
            "tiles_total": self.tiles_total,
            "percent": (
                round(100 * self.tiles_done / self.tiles_total) if self.tiles_total else 0
            ),
            "rows": self.rows,
            "duplicates": self.duplicates,
            "yielded": self.yielded,
            "message": self.message,
            "error": self.error,
        }


class Builder:
    """One build at a time, owned by the server, yielding to renders."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._progress = Progress()
        self._archive: Path | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def snapshot(self) -> dict[str, object]:
        return self._progress.as_dict()

    def start(self, kind: str, archive: Path) -> dict[str, object]:
        if kind not in KINDS:
            return {"started": False, "reason": f"There is nothing called {kind!r} to build."}

        with self._lock:
            if self._running:
                return {
                    "started": False,
                    "reason": f"Already building {self._progress.kind}.",
                    **self.snapshot(),
                }
            if not archive.is_file():
                return {"started": False, "reason": "No basemap is installed to read."}

            self._archive = archive
            self._stop.clear()
            self._running = True
            self._progress = Progress(
                kind=kind, state=BUILDING, message="Looking at the archive."
            )
            self._thread = threading.Thread(
                target=self._run, args=(kind,), name=f"irfaran-gazetteer-{kind}", daemon=True
            )
            self._thread.start()
            return {"started": True, **self.snapshot()}

    def stop(self) -> dict[str, object]:
        if not self._running:
            return {"stopping": False, "reason": "Nothing is being built.", **self.snapshot()}
        self._stop.set()
        self._progress.state = STOPPING
        self._progress.message = "Stopping after this batch. Resuming carries on from here."
        return {"stopping": True, **self.snapshot()}

    # -- the work -----------------------------------------------------------

    def _run(self, kind: str) -> None:
        conn = db.connect()
        began = time.monotonic()
        try:
            self._build(conn, kind)
        except Exception as exc:  # noqa: BLE001 - a worker must not die silently
            self._progress.state = FAILED
            self._progress.error = str(exc)
            with db.transaction(conn):
                conn.execute(
                    "UPDATE gazetteer_build SET state = 'failed', error = ? WHERE kind = ?",
                    (str(exc), kind),
                )
            history.record(conn, "error", "gazetteer", f"The {kind} build stopped: {exc}", {})
        finally:
            elapsed = round(time.monotonic() - began, 1)
            self._progress.message = self._progress.message or ""
            if self._progress.state == BUILDING:
                self._progress.state = IDLE
            self._running = False
            history.record(
                conn,
                "system",
                "gazetteer",
                f"{KINDS[kind]['label']}: {self._progress.rows:,} names in "
                f"{_duration(elapsed)}"
                + (" (stopped early)" if self._stop.is_set() else ""),
                {
                    "kind": kind,
                    "rows": self._progress.rows,
                    "duplicates": self._progress.duplicates,
                    "tiles": self._progress.tiles_done,
                    "seconds": elapsed,
                    "stopped": self._stop.is_set(),
                },
            )
            conn.close()

    def _build(self, conn: sqlite3.Connection, kind: str) -> None:
        assert self._archive is not None
        install(conn)

        about = KINDS[kind]
        zoom = int(about["zoom"])
        layer = str(about["layer"])

        row = conn.execute(
            "SELECT * FROM gazetteer_build WHERE kind = ?", (kind,)
        ).fetchone()

        resuming = bool(
            row
            and str(row["state"]) in (STOPPING, BUILDING)
            and str(row["archive"] or "") == provenance(self._archive)
        )
        generation = int(row["generation"]) if resuming and row else (
            (int(row["generation"]) if row else 0) + 1
        )
        cursor = int(row["cursor"]) if resuming and row else -1
        written = int(row["rows_written"]) if resuming and row else 0
        duplicates = int(row["duplicates"]) if resuming and row else 0

        with pmtiles.Archive(self._archive) as archive:
            first, last = archive.zoom_range(zoom)

            if not resuming:
                # A fresh generation starts clean, in case an older attempt at
                # this same number left rows behind.
                with db.transaction(conn):
                    conn.execute(
                        "DELETE FROM gazetteer WHERE generation = ? AND kind = ?",
                        (generation, kind),
                    )
                    conn.execute(
                        "INSERT INTO gazetteer_build "
                        "(kind, generation, zoom, cursor, tiles_done, tiles_total, "
                        " rows_written, duplicates, archive, state, started_at, error) "
                        "VALUES (?, ?, ?, -1, 0, 0, 0, 0, ?, 'building', ?, '') "
                        "ON CONFLICT(kind) DO UPDATE SET generation = excluded.generation, "
                        "zoom = excluded.zoom, cursor = -1, tiles_done = 0, tiles_total = 0, "
                        "rows_written = 0, duplicates = 0, archive = excluded.archive, "
                        "state = 'building', started_at = excluded.started_at, error = ''",
                        (
                            kind,
                            generation,
                            zoom,
                            provenance(self._archive),
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        ),
                    )

            self._progress.message = f"Counting the tiles at z{zoom}."
            total = sum(1 for _ in archive.entries_between(first, last))
            self._progress.tiles_total = total
            with db.transaction(conn):
                conn.execute(
                    "UPDATE gazetteer_build SET tiles_total = ?, state = 'building' "
                    "WHERE kind = ?",
                    (total, kind),
                )

            self._progress.message = f"Reading {total:,} tiles."
            recent: dict[tuple[str, float, float], None] = {}
            batch: list[tuple[str, str, str, float, float, int]] = []
            done = 0
            #: The last id actually finished, which is what a resume starts after.
            #: Tracked rather than read off the loop variable, which is unbound
            #: when there was nothing left to scan.
            reached = cursor

            for entry in archive.entries_between(max(first, cursor + 1), last):
                if self._stop.is_set():
                    break

                # An edit always wins. A render and a scan competing for the
                # cores means both crawl, and the person waiting is the one who
                # asked for the render.
                while renderq.queue.running and not self._stop.is_set():
                    self._progress.yielded = True
                    self._progress.message = "Waiting for the map to finish drawing."
                    time.sleep(1.0)
                if self._stop.is_set():
                    break
                if self._progress.yielded:
                    self._progress.yielded = False
                    self._progress.message = f"Reading {total:,} tiles."

                zoom_level, tile_x, tile_y = pmtiles.tile_id_to_zxy(entry.tile_id)
                for _layer, attrs, px, py, extent in mvt.points(
                    archive.blob(entry), {layer}
                ):
                    name = str(attrs.get("name") or "").strip()
                    if not name:
                        continue

                    lon, lat = mvt.lonlat(zoom_level, tile_x, tile_y, px, py, extent)
                    key = (name, round(lat, 4), round(lon, 4))
                    if key in recent:
                        duplicates += 1
                        continue
                    recent[key] = None
                    if len(recent) > RECENT_KEYS:
                        # Oldest first: what has scrolled out of the scan's
                        # neighbourhood cannot duplicate what is coming.
                        for old in list(recent)[: RECENT_KEYS // 4]:
                            del recent[old]

                    batch.append(
                        (name, kind, str(attrs.get("kind") or ""), lat, lon, generation)
                    )

                done += 1
                reached = entry.tile_id
                if len(batch) >= BATCH:
                    written += self._flush(
                        conn, kind, batch, reached, done, written, duplicates
                    )
                    batch = []

                self._progress.tiles_done = done
                self._progress.rows = written + len(batch)
                self._progress.duplicates = duplicates

            written += self._flush(conn, kind, batch, reached, done, written, duplicates)
            self._progress.rows = written

            if self._stop.is_set():
                with db.transaction(conn):
                    conn.execute(
                        "UPDATE gazetteer_build SET state = 'stopping' WHERE kind = ?", (kind,)
                    )
                self._progress.message = (
                    f"Stopped with {total - done:,} tiles left. Resuming carries on."
                )
                return

            # Complete: this generation becomes the one being searched, and the
            # one it replaced is dropped only now.
            previous = live_generation(conn, kind)
            with db.transaction(conn):
                _set_live(conn, kind, generation)
                if previous and previous != generation:
                    conn.execute(
                        "DELETE FROM gazetteer WHERE generation = ? AND kind = ?",
                        (previous, kind),
                    )
                conn.execute(
                    "UPDATE gazetteer_build SET state = 'done', finished_at = ?, "
                    "cursor = ? WHERE kind = ?",
                    (
                        datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        last,
                        kind,
                    ),
                )
            self._progress.message = f"{written:,} names from {done:,} tiles."

    def _flush(
        self,
        conn: sqlite3.Connection,
        kind: str,
        batch: list[tuple[str, str, str, float, float, int]],
        cursor: int,
        done: int,
        written: int,
        duplicates: int,
    ) -> int:
        """Write a batch and record how far the scan got. Returns rows written."""
        with db.transaction(conn):
            if batch:
                conn.executemany(
                    "INSERT INTO gazetteer (name, kind, category, lat, lon, generation) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    batch,
                )
            conn.execute(
                "UPDATE gazetteer_build SET cursor = ?, tiles_done = ?, "
                "rows_written = ?, duplicates = ? WHERE kind = ?",
                (cursor, done, written + len(batch), duplicates, kind),
            )
        return len(batch)


def look_up(
    conn: sqlite3.Connection,
    text: str,
    kinds: list[str],
    limit: int,
    inside: tuple[float, float, float, float] | None = None,
) -> list[dict[str, object]]:
    """Names matching `text`, in the kinds asked for.

    `inside` is (west, south, east, north) and restricts the answer to what is on
    screen. That matters more here than anywhere else: a pizzeria called Eleven is
    one of many on earth with that name, and the one somebody means is the one
    they are looking at.

    Duplicates are collapsed here as well as during the build. Labels are
    buffered into neighbouring tiles, and the scan only remembers a window of
    recent keys, so a few repeats reach the index.
    """
    query = _match_query(text)
    if not query or not kinds:
        return []

    live = {kind: live_generation(conn, kind) for kind in kinds}
    usable = [kind for kind, generation in live.items() if generation]
    if not usable:
        return []

    sql = (
        "SELECT name, kind, category, lat, lon FROM gazetteer "
        "WHERE gazetteer MATCH ? AND ("
        + " OR ".join("(kind = ? AND generation = ?)" for _ in usable)
        + ")"
    )
    params: list[object] = [query]
    for kind in usable:
        params.extend((kind, live[kind]))

    if inside is not None:
        west, south, east, north = inside
        sql += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
        params.extend((south, north, west, east))

    # Ranked by FTS relevance, then taken generously so collapsing repeats does
    # not leave a short list.
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit * 4)

    seen: set[tuple[str, float, float]] = set()
    out: list[dict[str, object]] = []
    for row in conn.execute(sql, params):
        key = (str(row["name"]), round(float(row["lat"]), 4), round(float(row["lon"]), 4))
        if key in seen:
            continue
        seen.add(key)

        category = str(row["category"] or "").replace("_", " ")
        out.append(
            {
                "kind": "gazetteer",
                "label": str(row["name"]),
                "detail": category.capitalize() if category else (
                    "Place" if row["kind"] == "place" else "Point of interest"
                ),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
            }
        )
        if len(out) >= limit:
            break
    return out


def _match_query(text: str) -> str:
    """What was typed, as an FTS5 prefix query.

    Quoted so punctuation cannot be read as FTS syntax - "Ai tre tini" would
    otherwise be three bare terms and a stray quote would be a parse error - and
    the last word gets a prefix star so it matches while it is still being typed.
    """
    words = [word for word in "".join(
        character if character.isalnum() or character.isspace() else " "
        for character in text
    ).split() if word]
    if not words:
        return ""
    quoted = [f'"{word}"' for word in words[:-1]]
    quoted.append(f'"{words[-1]}"*')
    return " ".join(quoted)


def _duration(seconds: float) -> str:
    if seconds < 60:
        return f"{round(seconds)}s"
    minutes = seconds / 60
    if minutes < 90:
        return f"{minutes:.0f} min"
    return f"{minutes / 60:.1f} h"


#: One per process, like the render queue.
builder = Builder()
