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

from fogmap import geo, raster
from fogmap.ingest import common

CATEGORIES = ("home", "school", "family", "holiday", "work", "other")
SOURCE = "place"


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


def _validate(payload: dict) -> tuple[str, str | None, list[str], float, float]:
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

    return name, category, people, lat, lon


def _stamp(
    conn: sqlite3.Connection,
    place_id: int,
    name: str,
    lat: float,
    lon: float,
    date_from: str | None,
    date_to: str | None,
    radius_m: float,
) -> int:
    """Create and rasterise the event that clears fog around a place."""
    layers = layers_for(date_from, date_to)
    cursor = conn.execute(
        "INSERT INTO events "
        "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
        "VALUES (?, 'add', ?, ?, ?, ?, ?, ?)",
        (
            SOURCE,
            json.dumps({"type": "Point", "coordinates": [lon, lat]}),
            radius_m,
            json.dumps(layers),
            f"place-{place_id}",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps({"place": name, "place_id": place_id}),
        ),
    )
    event_id = int(cursor.lastrowid)
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    raster.stamp_event(conn, row)
    return event_id


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


def as_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "people": json.loads(row["people"]) if row["people"] else [],
        "date_from": row["date_from"],
        "date_to": row["date_to"],
        "lat": row["lat"],
        "lon": row["lon"],
        "event_id": row["event_id"],
    }


def create(conn: sqlite3.Connection, payload: dict) -> tuple[dict[str, object], list[str]]:
    """Add a place and clear the fog around it. Returns the row and its layers."""
    name, category, people, lat, lon = _validate(payload)
    date_from = payload.get("date_from")
    date_to = payload.get("date_to")
    radius_m = float(payload.get("radius_m") or common.RADIUS_DEFAULTS_M[SOURCE])

    cursor = conn.execute(
        "INSERT INTO places (name, category, people, date_from, date_to, lat, lon) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (name, category, json.dumps(people), date_from, date_to, lat, lon),
    )
    place_id = int(cursor.lastrowid)

    event_id = _stamp(conn, place_id, name, lat, lon, date_from, date_to, radius_m)
    conn.execute("UPDATE places SET event_id = ? WHERE id = ?", (event_id, place_id))

    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    return as_dict(row), layers_for(date_from, date_to)


def update(
    conn: sqlite3.Connection, place_id: int, payload: dict
) -> tuple[dict[str, object], list[str], set[tuple[int, int]]]:
    """Change a place, re-stamping only if what it stamps actually changed."""
    existing = conn.execute(
        "SELECT * FROM places WHERE id = ?", (place_id,)
    ).fetchone()
    if existing is None:
        raise KeyError(place_id)

    merged = {**as_dict(existing), **payload}
    name, category, people, lat, lon = _validate(merged)
    date_from = merged.get("date_from")
    date_to = merged.get("date_to")
    radius_m = float(payload.get("radius_m") or common.RADIUS_DEFAULTS_M[SOURCE])

    conn.execute(
        "UPDATE places SET name = ?, category = ?, people = ?, date_from = ?, "
        "date_to = ?, lat = ?, lon = ? WHERE id = ?",
        (
            name,
            category,
            json.dumps(people),
            date_from,
            date_to,
            lat,
            lon,
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
        event_id = _stamp(
            conn, place_id, name, lat, lon, date_from, date_to, radius_m
        )
        conn.execute(
            "UPDATE places SET event_id = ? WHERE id = ?", (event_id, place_id)
        )
        dirty |= {geo.lonlat_to_tile(lon, lat)}

    row = conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
    return as_dict(row), layers_for(date_from, date_to), dirty


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
