# SPDX-License-Identifier: AGPL-3.0-or-later
"""SQLite schema, connections and idempotent initialisation.

No ORM. The event log in `events` is the only source of truth; `blobs` and
everything derived from it can be deleted and rebuilt byte for byte.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from irfaran import settings_env

DEFAULT_DATA_DIR = Path("/data")

# How long a write waits for another one to finish before giving up.
#
# SQLite allows one writer at a time. Python's default is five seconds, which
# is fine until a tracker delivers a few hundred buffered fixes at once - that
# is a single transaction rasterising every one of them, and anything arriving
# behind it loses. Thirty seconds is longer than any write here takes and
# still short enough that a genuine deadlock surfaces rather than hanging.
DEFAULT_BUSY_TIMEOUT_S = 30.0
DB_FILENAME = "irfaran.db"

# What the database was called before the project was renamed.
#
# An install that already has one keeps using it. Renaming somebody's archive
# out from under them to tidy up a filename is not a trade worth making, and
# the alternative - a fresh empty database beside a full one - looks exactly
# like losing everything.
LEGACY_DB_FILENAME = "fogmap.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,   -- ha | workout | manual | place
  op          TEXT NOT NULL,   -- add | reveal | erase
  geometry    TEXT NOT NULL,   -- GeoJSON Point, LineString or Polygon
  radius_m    REAL NOT NULL,
  layers      TEXT NOT NULL,   -- JSON array: ["2024"] | ["1994","1995"] | ["prehistory"]
  external_id TEXT,            -- dedup key, NULL for manual
  created_at  TEXT NOT NULL,   -- ISO8601
  meta        TEXT             -- JSON: activity name, device, accuracy, notes
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_dedup
  ON events(source, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS blobs (
  kind    TEXT NOT NULL,       -- fog | trail | erase
  source  TEXT NOT NULL,
  layer   TEXT NOT NULL,       -- "2024" | "prehistory"
  x       INTEGER NOT NULL,    -- z14 tile x
  y       INTEGER NOT NULL,
  data    BLOB NOT NULL,       -- raw numpy bytes, no compression
  PRIMARY KEY (kind, source, layer, x, y)
) WITHOUT ROWID;

-- A pin. name is its title; label, folder and tags are what the sidebar
-- organises it by. The older category/people/date columns are still here
-- because dropping a column means rebuilding the table, and they cost nothing
-- sitting empty.
CREATE TABLE IF NOT EXISTS places (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  category  TEXT,
  people    TEXT,
  date_from TEXT,
  date_to   TEXT,
  lat       REAL NOT NULL,
  lon       REAL NOT NULL,
  event_id  INTEGER REFERENCES events(id) ON DELETE SET NULL,
  label_id  INTEGER REFERENCES labels(id) ON DELETE SET NULL,
  folder_id INTEGER REFERENCES folders(id) ON DELETE SET NULL,
  tags      TEXT               -- JSON array
);

-- What a pin is, visually. Name and colour, defined once in settings and
-- pointed at by however many places share it.
CREATE TABLE IF NOT EXISTS labels (
  id     INTEGER PRIMARY KEY,
  name   TEXT NOT NULL,
  colour TEXT NOT NULL         -- #rrggbb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_labels_name ON labels(name);

-- Somewhere to put pins. parent_id nests them; visible hides a whole branch
-- from the map without deleting anything.
CREATE TABLE IF NOT EXISTS folders (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  parent_id INTEGER REFERENCES folders(id) ON DELETE CASCADE,
  visible   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- z14 tiles whose rendered PNGs are out of date.
--
-- Not part of section 5's schema, and not truth: it is a work queue over
-- derived state, and dropping it costs nothing a full rebuild cannot restore.
-- It exists because rendering after every single import is what makes a bulk
-- import take hours - the render is proportional to the whole archive, not to
-- the file just added. Deferring it means something has to remember what is
-- owed, and remembering it on the client loses the debt when the tab closes.
CREATE TABLE IF NOT EXISTS pending_render (
  x INTEGER NOT NULL,
  y INTEGER NOT NULL,
  PRIMARY KEY (x, y)
) WITHOUT ROWID;

-- Who you were with, for the Who? field on a pin.
--
-- A registry rather than the truth: a place stores the names themselves, in
-- places.people, so a pin remembers who was there even if the name is later
-- removed from this list. This is the list to choose from, which is what makes
-- multiple choice possible at all - free text cannot offer choices.
--
-- Renaming here does rewrite the name on every pin that carries it, because a
-- registry that disagrees with the pins is worse than no registry.
CREATE TABLE IF NOT EXISTS people (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS people_name ON people(name COLLATE NOCASE);

-- What happened, for the History tab.
--
-- Separate from the event log, which records what entered the map and cannot
-- express a failure, a render, or a setting being changed. It is also the only
-- place an error is kept at all: before this, a failed import existed in the
-- container's stdout and nowhere else, which a restart discards and the
-- interface cannot read.
--
-- Capped, because it is in the database somebody backs up. `count` exists so a
-- tracker delivering every few minutes is one line that grows rather than three
-- hundred lines a day that push out everything worth reading.
CREATE TABLE IF NOT EXISTS log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  category TEXT NOT NULL,
  action TEXT NOT NULL,
  message TEXT NOT NULL,
  count INTEGER NOT NULL DEFAULT 1,
  detail TEXT
);

CREATE INDEX IF NOT EXISTS log_at ON log(at DESC);
"""

DEFAULT_SETTINGS = {
    "ha_ingest_enabled": "false",
    "overland_ingest_enabled": "false",
    "owntracks_ingest_enabled": "false",
    "intervals_enabled": "false",
    "ui_theme": "system",
    "map_theme": "dark",
}


def data_dir() -> Path:
    """The directory holding the database, blobs, tiles and the basemap."""
    configured = settings_env.get("DATA_DIR")
    return Path(configured) if configured else DEFAULT_DATA_DIR


def busy_timeout_s() -> float:
    raw = settings_env.get("BUSY_TIMEOUT_S")
    if not raw:
        return DEFAULT_BUSY_TIMEOUT_S
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(
            f"IRFARAN_BUSY_TIMEOUT_S must be a number of seconds, got {raw!r}. "
            f"Unset it to use the default of {DEFAULT_BUSY_TIMEOUT_S}."
        ) from None


def basemap_dir() -> Path:
    """Where the PMTiles basemap lives.

    Its own setting because it is the one thing here that is enormous and
    entirely replaceable. A planet archive is around 137 GB of public map data
    that can be re-downloaded any time, while everything else in the data
    directory is irreplaceable and measured in megabytes - so on a machine
    with a fast pool and a slow array, they want to be on different disks.
    Unraid is the obvious case: appdata belongs on the cache, 137 GB does not.

    Defaults to the data directory, which is right for a single-disk install.
    """
    configured = settings_env.get("BASEMAP_DIR")
    return Path(configured) if configured else data_dir()


def db_path() -> Path:
    directory = data_dir()
    current = directory / DB_FILENAME
    if not current.exists():
        legacy = directory / LEGACY_DB_FILENAME
        if legacy.exists():
            return legacy
    return current


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the settings Irfaran relies on everywhere."""
    target = Path(path) if path is not None else db_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create the Irfaran data directory at {target.parent}. "
            f"The bind mount is missing or not writable ({exc}). On an "
            "SELinux host every bind mount needs a :z label."
        ) from exc

    try:
        # check_same_thread=False because FastAPI runs a sync dependency and
        # the route that uses it in whichever worker threads are free, and
        # they are often not the same one. The connection is still used by a
        # single request from start to finish, never by two at once, so the
        # check is guarding against something that cannot happen here - while
        # its absence made every endpoint fail under concurrent load, which a
        # page load produces every time.
        conn = sqlite3.connect(
            target,
            isolation_level=None,
            check_same_thread=False,
            timeout=busy_timeout_s(),
        )
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Cannot open the Irfaran database at {target} ({exc})."
        ) from exc

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added to tables that already existed before they were.
#
# CREATE TABLE IF NOT EXISTS does nothing to a table that is already there, so
# a column added later has to be added by hand. Kept as data rather than as a
# migration framework: Irfaran has one database, on one machine, and a list of
# (table, column, definition) is easier to read than anything that manages it.
MIGRATIONS = (
    ("places", "label_id", "INTEGER REFERENCES labels(id) ON DELETE SET NULL"),
    ("places", "folder_id", "INTEGER REFERENCES folders(id) ON DELETE SET NULL"),
    ("places", "tags", "TEXT"),
)


def init(conn: sqlite3.Connection) -> None:
    """Create the schema and seed defaults. Safe to run on every startup.

    Running this against a populated database changes nothing: every statement
    is guarded and settings are only inserted when absent, so a value the user
    changed is never reset.
    """
    conn.executescript(SCHEMA)

    for table, column, definition in MIGRATIONS:
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    conn.executemany(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
        sorted(DEFAULT_SETTINGS.items()),
    )


def open_initialised(path: Path | str | None = None) -> sqlite3.Connection:
    """Connect and ensure the schema exists."""
    conn = connect(path)
    init(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block as one transaction.

    Connections are opened in autocommit mode, so an import that fails halfway
    would otherwise leave half its events in the log. Rolling back keeps the
    event log consistent with what the user was told happened.
    """
    conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# Settings that are never handed out.
#
# The settings table is the obvious place to keep the API token, and
# /api/settings and /api/meta are both readable without one - so anything
# secret in here walks straight out of an endpoint that has no reason to
# refuse anybody. Filtered at the source rather than at each caller, because
# the next caller will not remember. tokens.py reads the row directly, which
# is the one place that should.
# Never served back out. The tracker key is somebody's credential on another
# service, which makes leaking it worse than leaking this server's own token.
SECRET_SETTINGS = frozenset({"api_token", "intervals_api_key"})


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {
        row["key"]: row["value"]
        for row in rows
        if row["key"] not in SECRET_SETTINGS
    }


def path_of(conn: sqlite3.Connection) -> str:
    """The file a connection is actually open on.

    Render workers are separate processes and cannot be handed a connection,
    so they are handed a path and open their own. Taking it from the
    connection rather than from db_path() means a caller working against some
    other database - a test, a one-off script - gets workers that agree with
    it, instead of seven processes quietly rendering production.
    """
    for _, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return str(file)
    return str(db_path())


PENDING_KINDS_KEY = "pending_render_kinds"


def defer_render(
    conn: sqlite3.Connection,
    tiles: set[tuple[int, int]],
    kinds: tuple[str, ...] = ("fog", "trail"),
) -> None:
    """Record z14 tiles whose PNGs are now out of date, and which of them.

    Recolouring the trails does not change a single fog pixel, and rendering
    the fog anyway is half the work for none of the result. What is owed is
    the union of every deferral since the last render, so an import that owes
    both followed by a recolour that owes one still owes both.
    """
    if not tiles:
        return
    conn.executemany(
        "INSERT INTO pending_render (x, y) VALUES (?, ?) ON CONFLICT DO NOTHING",
        sorted(tiles),
    )

    owed = _recorded_kinds(conn) | set(kinds)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (PENDING_KINDS_KEY, ",".join(sorted(owed))),
    )


def _recorded_kinds(conn: sqlite3.Connection) -> set[str]:
    """Exactly what is written down, which may be nothing at all."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (PENDING_KINDS_KEY,)
    ).fetchone()
    if not row or not str(row["value"]).strip():
        return set()
    return {part for part in str(row["value"]).split(",") if part}


def pending_kinds(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Which tile kinds are owed.

    Both when nothing was written down: a debt from before this was recorded,
    or from any path that did not say, has to be assumed to cover everything.
    Rendering more than was needed wastes time; rendering less leaves tiles
    silently wrong.
    """
    recorded = _recorded_kinds(conn)
    return tuple(sorted(recorded)) if recorded else ("fog", "trail")


def pending_render(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {
        (int(row["x"]), int(row["y"]))
        for row in conn.execute("SELECT x, y FROM pending_render")
    }


def clear_pending_render(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pending_render")
    conn.execute("DELETE FROM settings WHERE key = ?", (PENDING_KINDS_KEY,))


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Row counts used by selfcheck and /api/meta."""
    out: dict[str, int] = {}
    for table in ("events", "blobs", "places", "settings"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        out[table] = int(row["n"])
    return out


def blob_counts_by_kind(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM blobs GROUP BY kind ORDER BY kind"
    ).fetchall()
    return {row["kind"]: int(row["n"]) for row in rows}


def layer_inventory(conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Layers present in the blob store, with the sources contributing to each.

    Reads from `blobs` rather than parsing the JSON `layers` column on every
    event, because this is what actually has rendered pixels behind it.
    """
    rows = conn.execute(
        """
        SELECT layer, source, COUNT(*) AS n
        FROM blobs
        GROUP BY layer, source
        ORDER BY layer, source
        """
    ).fetchall()

    by_layer: dict[str, dict[str, object]] = {}
    for row in rows:
        entry = by_layer.setdefault(
            row["layer"], {"layer": row["layer"], "sources": [], "blobs": 0}
        )
        entry["sources"].append(row["source"])  # type: ignore[union-attr]
        entry["blobs"] = int(entry["blobs"]) + int(row["n"])  # type: ignore[arg-type]
    return list(by_layer.values())
