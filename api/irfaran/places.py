# SPDX-License-Identifier: AGPL-3.0-or-later
"""Named places.

A place is two things: a row people can read, and an event that clears fog
around it. The event goes through the same path as everything else, so a
place someone remembers is indistinguishable in the raster from a place a GPS
recorded.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from irfaran import geo, raster
from irfaran.ingest import common

CATEGORIES = ("home", "school", "family", "holiday", "work", "other")
SOURCE = "place"

# Dropping a pin clears the fog around it, and clears it without drawing a
# route: somebody was here, which is not the same claim as somebody walked
# along here. That is exactly what the reveal op is for.
OP = "reveal"


class PlaceError(ValueError):
    """Bad input, phrased for whoever typed it."""


def _people(raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        names = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        names = [str(part).strip() for part in raw]
    else:
        raise PlaceError(
            f"people must be a list of names or a comma separated string, "
            f"got {raw!r}."
        )
    return sorted(dict.fromkeys(name for name in names if name))


def _year(value: str | None) -> str | None:
    """The year part of a date, for deciding which layers a place belongs to."""
    if not value:
        return None
    text = str(value).strip()
    if len(text) >= 4 and text[:4].isdigit():
        return text[:4]
    raise PlaceError(
        f"Date {value!r} is not readable. Use an ISO date such as 1994-09-01, "
        "or just the year."
    )


def layers_for(date_from: str | None, date_to: str | None) -> list[str]:
    """Which time layers a place belongs to.

    A place someone lived in from 1994 to 2002 belongs to every one of those
    years. A place with no dates is prehistory, which is where undated
    memories go.
    """
    start, end = _year(date_from), _year(date_to)
    if start and end:
        return common.expand_layers([f"{start}..{end}"])
    if start:
        return [start]
    if end:
        return [end]
    return [common.PREHISTORY]


#: How prominent a pin is. Minor pins are drawn smaller and hidden when the
#: map is zoomed further out than MINOR_FROM_ZOOM, which is where a hundred
#: pins stop being information and start being confetti.
PROMINENCE = ("major", "minor")


def _prominence(raw: object) -> str:
    if raw is None:
        return "major"
    value = str(raw).strip().lower()
    if value not in PROMINENCE:
        raise PlaceError(
            f"prominence must be one of {', '.join(PROMINENCE)}, got {raw!r}."
        )
    return value


def _validate(
    payload: dict,
) -> tuple[str, str | None, list[str], float, float, str]:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise PlaceError("A place needs a name.")

    category = payload.get("category")
    if category is not None:
        category = str(category).strip().lower() or None
    if category is not None and category not in CATEGORIES:
        raise PlaceError(
            f"Category {category!r} is not one of {', '.join(CATEGORIES)}."
        )

    people = _people(payload.get("people"))

    try:
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlaceError(
            "A place needs numeric lat and lon. "
            f"Got lat={payload.get('lat')!r}, lon={payload.get('lon')!r}."
        ) from exc

    if not -90.0 <= lat <= 90.0:
        raise PlaceError(f"Latitude {lat} is outside -90 to 90.")
    if not -180.0 <= lon <= 180.0:
        raise PlaceError(f"Longitude {lon} is outside -180 to 180.")

    return name, category, people, lat, lon, _prominence(payload.get("prominence"))


def _stamp(
    conn: sqlite3.Connection,
    place_id: int,
    name: str,
    lat: float,
    lon: float,
    date_from: str | None,
    date_to: str | None,
    radius_m: float,
) -> tuple[int, set[tuple[int, int]]]:
    """Create and rasterise the event that clears fog around a place.

    A pin has exactly one fog event, named after it. Anything already holding
    that name is a leftover - most likely from a restore that brought the event
    but not the link back to the pin - and inserting alongside it fails on the
    UNIQUE index over (source, external_id), which is how the first edit of a
    restored pin came to answer 500. The leftover is removed and its ground
    returned for rebuilding, so the fog it drew does not survive it.
    """
    layers = layers_for(date_from, date_to)
    external_id = f"place-{place_id}"

    stale: set[tuple[int, int]] = set()
    previous = conn.execute(
        "SELECT * FROM events WHERE source = ? AND external_id = ?",
        (SOURCE, external_id),
    ).fetchone()
    if previous is not None:
        stale = raster.event_tiles(previous)
        conn.execute("DELETE FROM events WHERE id = ?", (previous["id"],))

    cursor = conn.execute(
        "INSERT INTO events "
        "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            SOURCE,
            OP,
            json.dumps({"type": "Point", "coordinates": [lon, lat]}),
            radius_m,
            json.dumps(layers),
            external_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps({"place": name, "place_id": place_id}),
        ),
    )
    event_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return event_id, raster.stamp_event(conn, row) | stale


def _drop_event(conn: sqlite3.Connection, event_id: int | None) -> set[tuple[int, int]]:
    """Remove a place's event and report the tiles that need rebuilding."""
    if event_id is None:
        return set()

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return set()

    tiles = raster.event_tiles(row)
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    return tiles


def _reference(
    conn: sqlite3.Connection, table: str, value: object, noun: str
) -> int | None:
    """Check a foreign key before storing it, and say so when it is wrong.

    SQLite would take an id pointing at nothing and only complain later, if at
    all. A pin filed under a folder that does not exist is a pin nobody will
    find again.
    """
    if value in (None, "", "none"):
        return None
    try:
        wanted = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise PlaceError(f"{noun}_id must be a number, got {value!r}.") from None

    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (wanted,)).fetchone()
    if row is None:
        raise PlaceError(f"There is no {noun} with id {wanted}.")
    return wanted


def _tags(raw: object) -> list[str]:
    """Tags from either a comma separated string or a list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        parts = [str(part).strip() for part in raw]
    else:
        raise PlaceError(
            f"tags must be a list or a comma separated string, got {raw!r}."
        )
    return sorted(dict.fromkeys(part for part in parts if part))


def as_dict(row: sqlite3.Row) -> dict[str, object]:
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "label_id": row["label_id"] if "label_id" in keys else None,
        "folder_id": row["folder_id"] if "folder_id" in keys else None,
        "tags": json.loads(row["tags"]) if "tags" in keys and row["tags"] else [],
        "category": row["category"],
        "people": json.loads(row["people"]) if row["people"] else [],
        "prominence": (
            row["prominence"] if "prominence" in row.keys() and row["prominence"]
            else "major"
        ),
        "date_from": row["date_from"],
        "date_to": row["date_to"],
        "lat": row["lat"],
        "lon": row["lon"],
        "event_id": row["event_id"],
    }


def create(
    conn: sqlite3.Connection, payload: dict
) -> tuple[dict[str, object], list[str], set[tuple[int, int]]]:
    """Add a place and clear the fog around it.

    Returns the row, its layers, and the z14 tiles the fog-clearing touched -
    the last so the caller can render that ground rather than the whole world.
    """
    name, category, people, lat, lon, prominence = _validate(payload)
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    radius_m = float(payload.get("radius_m") or common.RADIUS_DEFAULTS_M[SOURCE])

    cursor = conn.execute(
        "INSERT INTO places "
        "(name, category, people, date_from, date_to, lat, lon, "
        " label_id, folder_id, tags, prominence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            name,
            category,
            json.dumps(people),
            date_from,
            date_to,
            lat,
            lon,
            _reference(conn, "labels", payload.get("label_id"), "label"),
            _reference(conn, "folders", payload.get("folder_id"), "folder"),
            json.dumps(_tags(payload.get("tags"))),
            prominence,
        ),
    )
    place_id = int(cursor.lastrowid)

    event_id, tiles = _stamp(
        conn, place_id, name, lat, lon, date_from, date_to, radius_m
    )
    conn.execute("UPDATE places SET event_id = ? WHERE id = ?", (event_id, place_id))

    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    return as_dict(row), layers_for(date_from, date_to), tiles


def update(
    conn: sqlite3.Connection, place_id: int, payload: dict
) -> tuple[dict[str, object], list[str], list[str], set[tuple[int, int]]]:
    """Change a place, re-stamping only if what it stamps actually changed.

    Returns the row, the layers it belongs to now, the layers it belonged to
    before, and the tiles that need rebuilding. The previous layers matter:
    moving a place from 1995 to 2010 leaves 1995 holding fog that is no longer
    there, and nothing else would ever go back and look.
    """
    existing = conn.execute(
        "SELECT * FROM places WHERE id = ?", (place_id,)
    ).fetchone()
    if existing is None:
        raise KeyError(place_id)

    merged = {**as_dict(existing), **payload}
    name, category, people, lat, lon, prominence = _validate(merged)
    date_from = merged.get("date_from")
    date_to = merged.get("date_to")
    radius_m = float(payload.get("radius_m") or common.RADIUS_DEFAULTS_M[SOURCE])

    conn.execute(
        "UPDATE places SET name = ?, category = ?, people = ?, date_from = ?, "
        "date_to = ?, lat = ?, lon = ?, label_id = ?, folder_id = ?, tags = ?, "
        "prominence = ? WHERE id = ?",
        (
            name,
            category,
            json.dumps(people),
            date_from,
            date_to,
            lat,
            lon,
            _reference(conn, "labels", merged.get("label_id"), "label"),
            _reference(conn, "folders", merged.get("folder_id"), "folder"),
            json.dumps(_tags(merged.get("tags"))),
            prominence,
            place_id,
        ),
    )

    moved = (lat, lon) != (existing["lat"], existing["lon"])
    relayered = layers_for(date_from, date_to) != layers_for(
        existing["date_from"], existing["date_to"]
    )

    dirty: set[tuple[int, int]] = set()
    if moved or relayered or existing["event_id"] is None:
        dirty = _drop_event(conn, existing["event_id"])
        event_id, stamped = _stamp(
            conn, place_id, name, lat, lon, date_from, date_to, radius_m
        )
        dirty |= stamped
        conn.execute(
            "UPDATE places SET event_id = ? WHERE id = ?", (event_id, place_id)
        )
        dirty |= {geo.lonlat_to_tile(lon, lat)}

    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    return (
        as_dict(row),
        layers_for(date_from, date_to),
        layers_for(existing["date_from"], existing["date_to"]),
        dirty,
    )


def delete(
    conn: sqlite3.Connection, place_id: int
) -> tuple[dict[str, object], set[tuple[int, int]]]:
    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    if row is None:
        raise KeyError(place_id)

    tiles = _drop_event(conn, row["event_id"])
    conn.execute("DELETE FROM places WHERE id = ?", (place_id,))
    return as_dict(row), tiles


def listing(
    conn: sqlite3.Connection, person: str | None = None
) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM places ORDER BY name").fetchall()
    places = [as_dict(row) for row in rows]
    if not person:
        return places

    wanted = person.strip().casefold()
    return [
        place
        for place in places
        if any(str(name).casefold() == wanted for name in place["people"])  # type: ignore[union-attr]
    ]


def people(conn: sqlite3.Connection) -> list[str]:
    """Everyone named on any place, for the filter."""
    names: set[str] = set()
    for row in conn.execute("SELECT people FROM places WHERE people IS NOT NULL"):
        try:
            names.update(str(name) for name in json.loads(row["people"]))
        except (json.JSONDecodeError, TypeError):
            continue
    return sorted(names)
