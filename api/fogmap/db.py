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

DEFAULT_DATA_DIR = Path("/data")
DB_FILENAME = "fogmap.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY,
  source      TEXT NOT NULL,   -- ha | workout | manual | place
  op          TEXT NOT NULL,   -- add | erase
  geometry    TEXT NOT NULL,   -- GeoJSON LineString or Point
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

CREATE TABLE IF NOT EXISTS places (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,
  category  TEXT,              -- home | school | family | holiday | work | other
  people    TEXT,              -- JSON array of names
  date_from TEXT,
  date_to   TEXT,
  lat       REAL NOT NULL,
  lon       REAL NOT NULL,
  event_id  INTEGER REFERENCES events(id) ON DELETE SET NULL
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
"""

DEFAULT_SETTINGS = {
    "ha_ingest_enabled": "false",
    "overland_ingest_enabled": "false",
    "owntracks_ingest_enabled": "false",
    "ui_theme": "system",
    "map_theme": "dark",
}


def data_dir() -> Path:
    """The directory holding the database, blobs, tiles and the basemap."""
    configured = os.environ.get("FOGMAP_DATA_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_DATA_DIR


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with the settings FogMap relies on everywhere."""
    target = Path(path) if path is not None else db_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create the FogMap data directory at {target.parent}. "
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
        conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"Cannot open the FogMap database at {target} ({exc})."
        ) from exc

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Create the schema and seed defaults. Safe to run on every startup.

    Running this against a populated database changes nothing: every statement
    is guarded and settings are only inserted when absent, so a value the user
    changed is never reset.
    """
    conn.executescript(SCHEMA)
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


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
    return {row["key"]: row["value"] for row in rows}


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


def defer_render(conn: sqlite3.Connection, tiles: set[tuple[int, int]]) -> None:
    """Record z14 tiles whose PNGs are now out of date."""
    if not tiles:
        return
    conn.executemany(
        "INSERT INTO pending_render (x, y) VALUES (?, ?) ON CONFLICT DO NOTHING",
        sorted(tiles),
    )


def pending_render(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    return {
        (int(row["x"]), int(row["y"]))
        for row in conn.execute("SELECT x, y FROM pending_render")
    }


def clear_pending_render(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM pending_render")


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
