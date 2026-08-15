# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live tracking sources.

Overland, OwnTracks and Home Assistant all post single fixes or small batches
rather than files. One event per fix would give thousands of rows a day and a
map made of dots, so fixes append to a same-day open track per source: one
event, growing, with the points held in time order.

Every source is opt-in and off by default. Irfaran is entirely usable with none
of them configured - these are optional data sources, not dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from irfaran import raster
from irfaran.ingest.common import (
    Fix,
    RADIUS_DEFAULTS_M,
    drop_inaccurate,
    layer_for,
)

LIVE_SOURCES = ("ha", "overland", "owntracks")


class LiveError(ValueError):
    """Bad payload, phrased for whoever has to configure the tracker."""


@dataclass
class LiveResult:
    accepted: int = 0
    duplicates: int = 0
    dropped: int = 0
    event_id: int | None = None
    tiles_touched: set[tuple[int, int]] = field(default_factory=set)

    def as_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "dropped": self.dropped,
            "event_id": self.event_id,
            "tiles_touched": len(self.tiles_touched),
        }


def setting_key(source: str) -> str:
    return f"{source}_ingest_enabled"


def is_enabled(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (setting_key(source),)
    ).fetchone()
    return bool(row) and str(row["value"]).strip().lower() == "true"


def set_enabled(conn: sqlite3.Connection, source: str, enabled: bool) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (setting_key(source), "true" if enabled else "false"),
    )


def has_events(conn: sqlite3.Connection, source: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM events WHERE source = ? LIMIT 1", (source,)
    ).fetchone()
    return row is not None


# -- parsing -----------------------------------------------------------------


def _timestamp(value: object, what: str) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    text = str(value or "").strip()
    if not text:
        raise LiveError(f"{what} is missing a timestamp.")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveError(
            f"{what} has an unreadable timestamp {text!r}. Expected ISO 8601 "
            "or unix seconds."
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _coordinate(value: object, what: str) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise LiveError(f"{what} is not a number, got {value!r}.") from exc


def parse_overland(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """Overland posts {"locations": [GeoJSON Feature, ...]}.

    Batches arrive out of order after the phone has been offline, so nothing
    here assumes the points are chronological.
    """
    if not isinstance(payload, dict) or "locations" not in payload:
        raise LiveError(
            'Overland payloads look like {"locations": [...]}. '
            f"Got {type(payload).__name__}."
        )

    locations = payload.get("locations")
    if not isinstance(locations, list):
        raise LiveError('"locations" must be a list of GeoJSON features.')

    fixes: list[Fix] = []
    motions: set[str] = set()
    devices: set[str] = set()

    for index, feature in enumerate(locations, start=1):
        if not isinstance(feature, dict):
            raise LiveError(f"Overland location {index} is not an object.")

        geometry = feature.get("geometry") or {}
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
            raise LiveError(
                f"Overland location {index} has no [lon, lat] coordinates."
            )

        properties = feature.get("properties") or {}
        accuracy = properties.get("horizontal_accuracy")

        fixes.append(
            Fix(
                lon=_coordinate(coordinates[0], f"Overland location {index} longitude"),
                lat=_coordinate(coordinates[1], f"Overland location {index} latitude"),
                time=_timestamp(
                    properties.get("timestamp"), f"Overland location {index}"
                ),
                accuracy=None if accuracy is None else _coordinate(
                    accuracy, f"Overland location {index} accuracy"
                ),
            )
        )

        motion = properties.get("motion")
        if isinstance(motion, list):
            motions.update(str(item) for item in motion)
        elif motion:
            motions.add(str(motion))

        if properties.get("device_id"):
            devices.add(str(properties["device_id"]))

    meta: dict[str, object] = {}
    if motions:
        meta["motion"] = sorted(motions)
    if devices:
        meta["device"] = sorted(devices)
    return fixes, meta


def parse_owntracks(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """OwnTracks posts one object per request.

    Anything that is not a location report is ignored rather than refused -
    the app sends several message types down the same endpoint.
    """
    if payload is None or payload == "" or payload == {}:
        # OwnTracks posts an empty body when a friend is deleted. Not an error.
        return [], {}

    if not isinstance(payload, dict):
        raise LiveError(
            f"OwnTracks payloads are single JSON objects, got "
            f"{type(payload).__name__}."
        )

    if payload.get("_type") != "location":
        return [], {}

    fix = Fix(
        lon=_coordinate(payload.get("lon"), "OwnTracks longitude"),
        lat=_coordinate(payload.get("lat"), "OwnTracks latitude"),
        time=_timestamp(payload.get("tst"), "OwnTracks report"),
        accuracy=None
        if payload.get("acc") is None
        else _coordinate(payload.get("acc"), "OwnTracks accuracy"),
    )

    meta: dict[str, object] = {}
    headers = headers or {}
    device = headers.get("x-limit-d") or payload.get("tid")
    user = headers.get("x-limit-u")
    if device:
        meta["device"] = str(device)
    if user:
        meta["user"] = str(user)
    return [fix], meta


def parse_ha(
    payload: object, headers: dict[str, str] | None = None
) -> tuple[list[Fix], dict[str, object]]:
    """Home Assistant posts {lat, lon, accuracy, timestamp, device}."""
    if not isinstance(payload, dict):
        raise LiveError(
            f"Home Assistant payloads are JSON objects, got "
            f"{type(payload).__name__}."
        )

    accuracy = payload.get("accuracy")
    fix = Fix(
        lon=_coordinate(payload.get("lon"), "Home Assistant longitude"),
        lat=_coordinate(payload.get("lat"), "Home Assistant latitude"),
        time=_timestamp(payload.get("timestamp"), "Home Assistant report"),
        accuracy=None
        if accuracy in (None, "", "unknown")
        else _coordinate(accuracy, "Home Assistant accuracy"),
    )

    meta: dict[str, object] = {}
    if payload.get("device"):
        meta["device"] = str(payload["device"])
    return [fix], meta


PARSERS = {
    "overland": parse_overland,
    "owntracks": parse_owntracks,
    "ha": parse_ha,
}


# -- the open track ----------------------------------------------------------


def track_id(source: str, day: str) -> str:
    return f"live-{day}"


def _open_track(
    conn: sqlite3.Connection, source: str, day: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM events WHERE source = ? AND external_id = ?",
        (source, track_id(source, day)),
    ).fetchone()


def append(
    conn: sqlite3.Connection,
    source: str,
    fixes: list[Fix],
    meta: dict[str, object] | None = None,
    radius_m: float | None = None,
) -> LiveResult:
    """Add fixes to the day's open track, one event per source per day.

    Points are held in time order and deduplicated on their timestamp, so a
    batch replayed after an offline spell changes nothing and a batch that
    arrives late is inserted where it belongs rather than appended to the end.
    """
    if source not in LIVE_SOURCES:
        raise LiveError(
            f"Unknown live source {source!r}. Valid sources are "
            f"{', '.join(LIVE_SOURCES)}."
        )

    result = LiveResult()
    if not fixes:
        return result

    kept, dropped = drop_inaccurate(fixes)
    result.dropped = dropped
    if not kept:
        return result

    radius = RADIUS_DEFAULTS_M[source] if radius_m is None else radius_m

    # A batch can straddle midnight after an offline spell, so group by day
    # rather than assuming one batch belongs to one track.
    by_day: dict[str, list[Fix]] = {}
    for fix in kept:
        stamp = (fix.time or datetime.now(timezone.utc)).astimezone(timezone.utc)
        by_day.setdefault(stamp.strftime("%Y-%m-%d"), []).append(fix)

    for day, day_fixes in sorted(by_day.items()):
        result.tiles_touched |= _append_day(
            conn, source, day, day_fixes, meta or {}, radius, result
        )
    return result


def _append_day(
    conn: sqlite3.Connection,
    source: str,
    day: str,
    fixes: list[Fix],
    meta: dict[str, object],
    radius_m: float,
    result: LiveResult,
) -> set[tuple[int, int]]:
    existing = _open_track(conn, source, day)

    points: list[tuple[str, float, float]] = []
    if existing is not None:
        stored = json.loads(existing["meta"] or "{}")
        # The day's first fix is stored as a Point and everything after it as
        # a LineString, so read it back through the helper that knows both.
        coordinates = raster.geometry_points(
            existing["geometry"], int(existing["id"])
        )
        for stamp, (lon, lat) in zip(stored.get("timestamps", []), coordinates):
            points.append((stamp, lon, lat))
    else:
        stored = {}

    seen = {stamp for stamp, _, _ in points}
    before = len(points)

    for fix in fixes:
        stamp = (
            (fix.time or datetime.now(timezone.utc))
            .astimezone(timezone.utc)
            .isoformat(timespec="seconds")
        )
        if stamp in seen:
            result.duplicates += 1
            continue
        seen.add(stamp)
        points.append((stamp, fix.lon, fix.lat))

    if len(points) == before:
        return set()

    points.sort(key=lambda item: item[0])
    result.accepted += len(points) - before

    merged: dict[str, object] = {**stored, **meta}
    merged["timestamps"] = [stamp for stamp, _, _ in points]
    merged["live"] = True
    merged["fixes"] = len(points)

    geometry = json.dumps(
        {"type": "Point", "coordinates": [points[0][1], points[0][2]]}
        if len(points) == 1
        else {
            "type": "LineString",
            "coordinates": [[lon, lat] for _, lon, lat in points],
        }
    )
    layers = json.dumps([layer_for([Fix(lon=0.0, lat=0.0, time=_parse_day(day))])])

    if existing is None:
        cursor = conn.execute(
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES (?, 'add', ?, ?, ?, ?, ?, ?)",
            (
                source,
                geometry,
                radius_m,
                layers,
                track_id(source, day),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                json.dumps(merged),
            ),
        )
        event_id = int(cursor.lastrowid)
    else:
        event_id = int(existing["id"])
        conn.execute(
            "UPDATE events SET geometry = ?, meta = ?, layers = ? WHERE id = ?",
            (geometry, json.dumps(merged), layers, event_id),
        )

    result.event_id = event_id
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    # The track changed shape, so its tiles are rebuilt from the event log
    # rather than having the new stretch added on top. Adding would inflate
    # the trail count on every batch and drift away from what a full rebuild
    # produces, which invariant 1 does not allow.
    tiles = raster.event_tiles(row)
    raster.rebuild_tiles(conn, tiles)
    return tiles


def _parse_day(day: str) -> datetime:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
