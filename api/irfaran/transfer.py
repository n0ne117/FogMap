# SPDX-License-Identifier: AGPL-3.0-or-later
"""Taking your archive somewhere else.

Only truth travels. The event log is the whole of it - every blob, every tile
and every pixel is derived from those rows and is rebuilt on arrival, so an
export is a few megabytes of JSON rather than a copy of a hundred-megabyte
cache. The basemap does not travel either: it is public map data that the new
instance can fetch for itself, and it is 137 GB.

Three things are deliberately left behind:

  the API token   belongs to the server, not to the archive. Carrying it would
                  silently give the old machine's key to the new one.
  setup state     the new instance has its own first run to do.
  pending renders a note about work owed on tiles that are not being exported.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import datetime, timezone
from irfaran import __version__, db

# The container format. A zip of newline-delimited JSON: openable with tools
# everybody already has, diffable, and streamable a row at a time so exporting
# a large archive never builds the whole thing in memory.
MANIFEST = "manifest.json"
SUFFIX = ".irfaran"

EVENTS = "events.ndjson"
TABLES = ("places", "labels", "folders", "people")

# Settings worth carrying: how the map looks, and how imports behave. Not the
# token, not setup state, and not which live trackers are switched on - that
# is a decision about this server, not about the history.
PORTABLE_SETTINGS = frozenset(
    {
        "ui_theme",
        "map_theme",
        "trail_ramp",
        "fog_colour_dark",
        "fog_colour_light",
    }
)

EVENT_COLUMNS = (
    "source",
    "op",
    "geometry",
    "radius_m",
    "layers",
    "external_id",
    "created_at",
    "meta",
)


class TransferError(ValueError):
    """Bad input, phrased for whoever is holding the file."""


# --------------------------------------------------------------------- export


def manifest(conn: sqlite3.Connection) -> dict[str, object]:
    counts = {
        name: int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
        for name in ("events", *TABLES)
    }
    return {
        "format": 1,
        "irfaran": __version__,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
    }


def export_bytes(conn: sqlite3.Connection) -> bytes:
    """An archive of everything worth keeping.

    Built in memory and returned whole rather than streamed. A zip's index
    lives at the end of the file so there is nothing to send early anyway, and
    a few megabytes of JSON does not need a temporary file. Returning bytes
    also keeps the work inside the request, where the database connection
    still exists - a streaming response runs after the handler returns, by
    which point the connection it was handed has been closed.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST, json.dumps(manifest(conn), indent=2))

        rows = conn.execute(
            f"SELECT {', '.join(EVENT_COLUMNS)} FROM events ORDER BY id"
        )
        archive.writestr(
            EVENTS,
            "".join(json.dumps(dict(row), sort_keys=True) + "\n" for row in rows),
        )

        for table in TABLES:
            archive.writestr(
                f"{table}.json",
                json.dumps(
                    [dict(row) for row in conn.execute(f"SELECT * FROM {table}")],
                    indent=2,
                    sort_keys=True,
                ),
            )

        archive.writestr(
            "settings.json",
            json.dumps(
                {
                    key: value
                    for key, value in db.get_settings(conn).items()
                    if key in PORTABLE_SETTINGS
                },
                indent=2,
                sort_keys=True,
            ),
        )

    return buffer.getvalue()


def export_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    return f"irfaran-{stamp}{SUFFIX}"


# --------------------------------------------------------------------- import


def read_archive(payload: bytes) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile:
        raise TransferError(
            "That is not an Irfaran export. An export is a zip archive with "
            f"a {MANIFEST} inside it."
        ) from None

    if MANIFEST not in archive.namelist():
        raise TransferError(
            f"That zip has no {MANIFEST} in it, so it is not an Irfaran export."
        )

    head = json.loads(archive.read(MANIFEST))
    if head.get("format") != 1:
        raise TransferError(
            f"That export is format {head.get('format')!r}, and this version "
            "reads format 1."
        )
    return archive


def dedup_key(origin: str, index: int, external_id: object) -> str:
    """A stable dedup key for an imported event.

    Events already carrying one keep it, so re-importing the same workout file
    on either instance still deduplicates. Hand-drawn events have none - there
    was nothing to deduplicate against - so one is synthesised from the export
    they came in, which makes importing the same archive twice a no-op instead
    of doubling every stroke somebody drew.
    """
    if external_id:
        return str(external_id)
    return f"import:{origin}:{index}"


def import_archive(
    conn: sqlite3.Connection, payload: bytes
) -> dict[str, object]:
    """Merge an export into this instance. Additive, and safe to repeat.

    Nothing is deleted and nothing is overwritten: an import adds what is not
    already here. Running the same file twice changes nothing the second time.
    """
    archive = read_archive(payload)
    head = json.loads(archive.read(MANIFEST))
    origin = str(head.get("exported_at", "unknown"))

    added = {"events": 0, "places": 0, "labels": 0, "folders": 0, "people": 0, "settings": 0}
    skipped = {"events": 0}

    labels = _merge_named(conn, archive, "labels", added)
    _merge_people(conn, archive, added)
    folders = _merge_folders(conn, archive, labels, added)

    if EVENTS in archive.namelist():
        for index, line in enumerate(archive.read(EVENTS).decode().splitlines()):
            if not line.strip():
                continue
            row = json.loads(line)
            key = dedup_key(origin, index, row.get("external_id"))

            existing = conn.execute(
                "SELECT id FROM events WHERE source = ? AND external_id = ?",
                (row.get("source"), key),
            ).fetchone()
            if existing:
                skipped["events"] += 1
                continue

            conn.execute(
                "INSERT INTO events "
                "(source, op, geometry, radius_m, layers, external_id, "
                " created_at, meta) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    row.get("source"),
                    row.get("op"),
                    row.get("geometry"),
                    row.get("radius_m"),
                    row.get("layers"),
                    key,
                    row.get("created_at"),
                    row.get("meta"),
                ),
            )
            added["events"] += 1

    _merge_places(conn, archive, labels, folders, added)

    if "settings.json" in archive.namelist():
        for key, value in json.loads(archive.read("settings.json")).items():
            if key not in PORTABLE_SETTINGS:
                continue
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )
            added["settings"] += 1

    return {"added": added, "skipped": skipped, "manifest": head}


def _merge_named(
    conn: sqlite3.Connection, archive: zipfile.ZipFile, table: str, added: dict
) -> dict[int, int]:
    """Merge labels by name, returning old id -> new id."""
    mapping: dict[int, int] = {}
    if f"{table}.json" not in archive.namelist():
        return mapping

    for row in json.loads(archive.read(f"{table}.json")):
        existing = conn.execute(
            f"SELECT id FROM {table} WHERE name = ?", (row.get("name"),)
        ).fetchone()
        if existing:
            mapping[int(row["id"])] = int(existing["id"])
            continue

        cursor = conn.execute(
            "INSERT INTO labels (name, colour) VALUES (?, ?)",
            (row.get("name"), row.get("colour")),
        )
        mapping[int(row["id"])] = int(cursor.lastrowid)
        added[table] += 1
    return mapping


def _merge_people(
    conn: sqlite3.Connection, archive: zipfile.ZipFile, added: dict
) -> None:
    """Merge the who-was-there registry by name.

    No id mapping, unlike labels and folders: a place stores the names
    themselves, so nothing points at a row here and the ids need not survive
    the journey.
    """
    if "people.json" not in archive.namelist():
        return

    for row in json.loads(archive.read("people.json")):
        name = str(row.get("name", "")).strip()
        if not name:
            continue
        existing = conn.execute(
            "SELECT id FROM people WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if existing:
            continue
        conn.execute("INSERT INTO people (name) VALUES (?)", (name,))
        added["people"] += 1


def _merge_folders(
    conn: sqlite3.Connection, archive: zipfile.ZipFile, labels: dict, added: dict
) -> dict[int, int]:
    """Merge folders by name and parent, returning old id -> new id.

    Parents first, so a subfolder always has somewhere to land.
    """
    mapping: dict[int, int] = {}
    if "folders.json" not in archive.namelist():
        return mapping

    rows = json.loads(archive.read("folders.json"))
    for row in sorted(rows, key=lambda item: item.get("parent_id") is not None):
        parent = mapping.get(row.get("parent_id")) if row.get("parent_id") else None
        existing = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS ?",
            (row.get("name"), parent),
        ).fetchone()
        if existing:
            mapping[int(row["id"])] = int(existing["id"])
            continue

        cursor = conn.execute(
            "INSERT INTO folders (name, parent_id, visible) VALUES (?, ?, ?)",
            (row.get("name"), parent, 1 if row.get("visible", 1) else 0),
        )
        mapping[int(row["id"])] = int(cursor.lastrowid)
        added["folders"] += 1
    return mapping


def _merge_places(
    conn: sqlite3.Connection,
    archive: zipfile.ZipFile,
    labels: dict,
    folders: dict,
    added: dict,
) -> None:
    """Add pins that are not already here, matched on name and position."""
    if "places.json" not in archive.namelist():
        return

    for row in json.loads(archive.read("places.json")):
        existing = conn.execute(
            "SELECT id FROM places WHERE name = ? AND lat = ? AND lon = ?",
            (row.get("name"), row.get("lat"), row.get("lon")),
        ).fetchone()
        if existing:
            continue

        conn.execute(
            "INSERT INTO places "
            "(name, category, people, date_from, date_to, lat, lon, "
            " label_id, folder_id, tags, prominence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("name"),
                row.get("category"),
                row.get("people"),
                row.get("date_from"),
                row.get("date_to"),
                row.get("lat"),
                row.get("lon"),
                labels.get(row.get("label_id")),
                folders.get(row.get("folder_id")),
                row.get("tags"),
                row.get("prominence") or "major",
            ),
        )
        added["places"] += 1
