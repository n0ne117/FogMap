# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared ingest path.

Every source - GPX, TCX, a live tracker, a freehand brush stroke - converts to
a list of fixes and then takes exactly this path: drop the inaccurate ones,
split on gaps, write one event per segment, rasterise. Manual drawing is not a
special case.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fogmap import raster

# Section 5. Radius is stored per event, never read from a global at render
# time - these are only the defaults used when an event is created.
#
# `workout` is 20 m rather than the 15 m in section 5, tuned by eye against a
# rendered map. The fog corridor has to be wide enough to read the basemap
# inside it, while the trail down the middle stays thin.
RADIUS_DEFAULTS_M = {
    "workout": 20.0,
    "ha": 30.0,
    "overland": 20.0,
    "owntracks": 20.0,
    "manual": 15.0,
    "place": 30.0,
}

# A tracker that stops logging over lunch and resumes across town would
# otherwise draw a straight line through everything between. The same applies,
# far more dramatically, to a flight.
DEFAULT_GAP_SECONDS = 300.0
DEFAULT_GAP_METRES = 1000.0

# Indoor and underground fixes routinely report 100 m or worse and would stamp
# fog over a whole block from someone sitting still.
DEFAULT_MAX_ACCURACY_M = 50.0

PREHISTORY = "prehistory"
EARTH_RADIUS_M = 6_371_008.8


def _env_float(name: str, fallback: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"{name} must be a number, got {raw!r}. Unset it to use the "
            f"default of {fallback}."
        ) from None


def gap_seconds() -> float:
    return _env_float("FOGMAP_GAP_SECONDS", DEFAULT_GAP_SECONDS)


def gap_metres() -> float:
    return _env_float("FOGMAP_GAP_METRES", DEFAULT_GAP_METRES)


def max_accuracy_m() -> float:
    return _env_float("FOGMAP_MAX_ACCURACY_M", DEFAULT_MAX_ACCURACY_M)


@dataclass(frozen=True)
class Fix:
    """One position report."""

    lon: float
    lat: float
    time: datetime | None = None
    accuracy: float | None = None


@dataclass
class Track:
    """A named sequence of fixes from one source file or one device session."""

    name: str
    fixes: list[Fix]
    activity: str | None = None
    device: str | None = None
    source_id: str | None = None


@dataclass
class IngestResult:
    events_created: int = 0
    events_skipped: int = 0
    points: int = 0
    points_dropped: int = 0
    tiles_touched: set[tuple[int, int]] = field(default_factory=set)
    layers: set[str] = field(default_factory=set)

    def as_dict(self) -> dict[str, int]:
        return {
            "events_created": self.events_created,
            "events_skipped": self.events_skipped,
            "points": self.points,
            "points_dropped": self.points_dropped,
            "tiles_touched": len(self.tiles_touched),
        }

    def affected_views(self) -> list[str]:
        """Canonical views this import changed, and only those.

        The cumulative view always changes; the year views only change for
        years the import actually put points in.
        """
        views = ["all"]
        views += sorted(f"year:{layer}" for layer in self.layers if layer.isdigit())
        if PREHISTORY in self.layers:
            views.append(PREHISTORY)
        return views


def haversine_m(lon_a: float, lat_a: float, lon_b: float, lat_b: float) -> float:
    """Great-circle distance in metres. Used only for gap detection."""
    phi_a, phi_b = math.radians(lat_a), math.radians(lat_b)
    d_phi = phi_b - phi_a
    d_lambda = math.radians(lon_b - lon_a)
    inner = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi_a) * math.cos(phi_b) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(inner)))


def drop_inaccurate(
    fixes: list[Fix], limit_m: float | None = None
) -> tuple[list[Fix], int]:
    """Remove fixes whose reported accuracy is worse than the limit.

    Fixes with no accuracy reported are kept - most GPX files carry none, and
    discarding them would discard the entire archive.
    """
    limit = max_accuracy_m() if limit_m is None else limit_m
    kept = [
        fix for fix in fixes if fix.accuracy is None or fix.accuracy <= limit
    ]
    return kept, len(fixes) - len(kept)


def segment(
    fixes: list[Fix],
    seconds: float | None = None,
    metres: float | None = None,
) -> list[list[Fix]]:
    """Split a track wherever the trace jumps in time or in space."""
    if not fixes:
        return []

    limit_s = gap_seconds() if seconds is None else seconds
    limit_m = gap_metres() if metres is None else metres

    segments: list[list[Fix]] = []
    current = [fixes[0]]

    for previous, fix in zip(fixes, fixes[1:]):
        jumped = haversine_m(previous.lon, previous.lat, fix.lon, fix.lat) > limit_m

        if not jumped and previous.time is not None and fix.time is not None:
            jumped = abs((fix.time - previous.time).total_seconds()) > limit_s

        if jumped:
            segments.append(current)
            current = [fix]
        else:
            current.append(fix)

    segments.append(current)
    return segments


def expand_layers(layers: list[str] | None) -> list[str]:
    """Expand a layer list, turning `1994..2002` into every year in between.

    Reconstructing where someone lived for eight years is one stroke, not
    eight, so a range is written to each year it covers.
    """
    if not layers:
        return [PREHISTORY]

    out: list[str] = []
    for raw in layers:
        layer = str(raw).strip()
        if not layer:
            continue

        if ".." not in layer:
            out.append(layer)
            continue

        start_text, _, end_text = layer.partition("..")
        start_text, end_text = start_text.strip(), end_text.strip()
        if not (start_text.isdigit() and end_text.isdigit()):
            raise ValueError(
                f"Layer range {layer!r} must be two years, as in '1994..2002'."
            )

        start, end = int(start_text), int(end_text)
        if start > end:
            raise ValueError(
                f"Layer range {layer!r} runs backwards. Write it as "
                f"'{end_text}..{start_text}'."
            )
        if end - start > 200:
            raise ValueError(
                f"Layer range {layer!r} spans {end - start + 1} years. That is "
                "almost certainly a typo."
            )
        out.extend(f"{year:04d}" for year in range(start, end + 1))

    if not out:
        return [PREHISTORY]

    # Stable and deduplicated, so the same stroke always stores the same list.
    return sorted(dict.fromkeys(out))


def layer_for(fixes: list[Fix]) -> str:
    """The time layer a segment belongs to.

    Derived from the first timestamp in the segment. Undated data - hand-drawn
    routes, imports with no clock - goes to `prehistory`.
    """
    for fix in fixes:
        if fix.time is not None:
            return f"{fix.time.year:04d}"
    return PREHISTORY


def geometry_for(fixes: list[Fix]) -> str:
    """GeoJSON for a segment. A lone fix is a Point, anything longer a LineString."""
    if len(fixes) == 1:
        return json.dumps(
            {"type": "Point", "coordinates": [fixes[0].lon, fixes[0].lat]}
        )
    return json.dumps(
        {
            "type": "LineString",
            "coordinates": [[fix.lon, fix.lat] for fix in fixes],
        }
    )


def external_id_for(track: Track, fixes: list[Fix], index: int) -> str:
    """A dedup key that survives re-export as well as re-upload.

    The segment's own start time comes first, because it is the one identifier
    every format agrees on: the same activity exported twice, or exported as
    both GPX and TCX, produces the same key and dedups. A format-specific
    identifier is only a fallback, and undated tracks fall back again to a
    hash of their own geometry.
    """
    for fix in fixes:
        if fix.time is not None:
            stamp = fix.time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            return f"{stamp}#{index}"

    if track.source_id:
        return f"{track.source_id}#{index}"

    digest = hashlib.sha256(geometry_for(fixes).encode("utf-8")).hexdigest()
    return f"sha256-{digest[:16]}#{index}"


def store_segment(
    conn: sqlite3.Connection,
    source: str,
    fixes: list[Fix],
    radius_m: float,
    layers: list[str],
    external_id: str | None,
    meta: dict[str, object] | None = None,
    op: str = "add",
) -> int | None:
    """Insert one event, or return None if it is already in the log.

    Dedup happens in SQLite via the partial unique index on
    (source, external_id), so a concurrent import cannot slip past it.
    """
    cursor = conn.execute(
        "INSERT OR IGNORE INTO events "
        "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            op,
            geometry_for(fixes),
            radius_m,
            json.dumps(layers),
            external_id,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            json.dumps(meta) if meta else None,
        ),
    )
    if cursor.rowcount == 0:
        return None
    return int(cursor.lastrowid)


def ingest_tracks(
    conn: sqlite3.Connection,
    source: str,
    tracks: list[Track],
    radius_m: float | None = None,
) -> IngestResult:
    """Run parsed tracks through the whole path and rasterise what is new."""
    if source not in RADIUS_DEFAULTS_M:
        raise ValueError(
            f"Unknown source {source!r}. Valid sources are "
            f"{', '.join(sorted(RADIUS_DEFAULTS_M))}."
        )
    radius = RADIUS_DEFAULTS_M[source] if radius_m is None else radius_m

    result = IngestResult()

    for track in tracks:
        kept, dropped = drop_inaccurate(track.fixes)
        result.points_dropped += dropped

        for index, part in enumerate(segment(kept)):
            if not part:
                continue

            layer = layer_for(part)
            event_id = store_segment(
                conn,
                source=source,
                fixes=part,
                radius_m=radius,
                layers=[layer],
                external_id=external_id_for(track, part, index),
                meta=_meta_for(track, part),
            )
            if event_id is None:
                result.events_skipped += 1
                continue

            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            result.tiles_touched |= raster.stamp_event(conn, row)
            result.events_created += 1
            result.points += len(part)
            result.layers.add(layer)

    return result


def _meta_for(track: Track, fixes: list[Fix]) -> dict[str, object]:
    meta: dict[str, object] = {"track": track.name, "fixes": len(fixes)}
    if track.activity:
        meta["activity"] = track.activity
    if track.device:
        meta["device"] = track.device

    started = next((fix.time for fix in fixes if fix.time is not None), None)
    if started is not None:
        meta["started_at"] = started.astimezone(timezone.utc).isoformat()
    return meta
