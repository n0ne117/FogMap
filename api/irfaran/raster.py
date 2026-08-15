# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brush stamping and blob storage.

All rasterisation happens here, at ingest time. Nothing in this module is ever
called from a tile request - that is invariant 3, and the whole reason the map
stays fast as the archive grows.

Blob dtypes, per section 5 of the build plan:
  fog, erase  uint8 256x256 used as a 0/255 mask
  trail       uint8 pass count, saturating at 255
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from functools import lru_cache
from typing import Iterable, Iterator, Sequence

import numpy as np
from PIL import Image, ImageDraw

from irfaran import geo, settings_env

TILE = geo.TILE_PX
MASK_ON = 255

# Resampled points are spaced at this fraction of the brush radius. Discs that
# overlap by half their radius leave no gaps along the stroke.
STEP_FRACTION = 0.5
MIN_STEP_PX = 0.5

# Erase applies to every layer, not just the one it was drawn in. A stroke that
# fixes GPS drift is fixing it for all time.
ERASE_LAYER = "*"

# Fog and trail are stamped at different widths on purpose. Fog clears a
# corridor wide enough to read the map inside; the trail is a line drawn down
# the middle of it. Stamping both at the fog radius made the cleared ground and
# the trail exactly the same shape, so the basemap never showed through.
#
# This is a ceiling, not a radius. The per-event radius still governs the fog,
# which is what invariant 4 is about.
#
# Three metres is a hair under half a pixel at z14 in the mid latitudes, which
# the kernel floors to a single pixel: the thinnest line the native grid can
# hold. Anything wider is magnified along with everything else once the client
# overzooms past z14, and a track drawn four pixels wide there reads as a road.
DEFAULT_TRAIL_MAX_RADIUS_M = 3.0


def trail_max_radius_m() -> float:
    raw = settings_env.get("TRAIL_MAX_RADIUS_M")
    if not raw:
        return DEFAULT_TRAIL_MAX_RADIUS_M
    try:
        return float(raw)
    except ValueError:
        raise ValueError(
            f"IRFARAN_TRAIL_MAX_RADIUS_M must be a number, got {raw!r}. Unset it "
            f"to use the default of {DEFAULT_TRAIL_MAX_RADIUS_M} m."
        ) from None

Tiles = dict[tuple[int, int], np.ndarray]


@lru_cache(maxsize=512)
def disc_kernel(radius_px_rounded: float) -> np.ndarray:
    """A filled circle as a boolean array, cached by radius.

    Radii are rounded before they reach here so that a track crossing a
    latitude band reuses kernels instead of rebuilding one per point.
    """
    radius = max(float(radius_px_rounded), 0.5)
    size = int(math.ceil(radius)) * 2 + 1
    centre = size // 2
    rows, cols = np.ogrid[:size, :size]
    return ((cols - centre) ** 2 + (rows - centre) ** 2) <= radius * radius


def _kernel_for(radius_px: float) -> np.ndarray:
    return disc_kernel(round(max(radius_px, 0.5), 2))


def paint(
    tiles: Tiles,
    x_px: float,
    y_px: float,
    kernel: np.ndarray,
    zoom: int = geo.NATIVE_Z,
) -> None:
    """OR a kernel into the tile mosaic at a world-pixel position.

    Writes across tile boundaries, allocating tiles on first touch. Anything
    falling outside the world grid is clipped rather than wrapped.
    """
    size = kernel.shape[0]
    centre = size // 2
    left = int(round(x_px)) - centre
    top = int(round(y_px)) - centre
    span = geo.world_px(zoom)
    across = 2**zoom

    if left + size <= 0 or top + size <= 0:
        return
    if left >= span or top >= span:
        return

    for tile_x in range(left // TILE, (left + size - 1) // TILE + 1):
        if not 0 <= tile_x < across:
            continue
        for tile_y in range(top // TILE, (top + size - 1) // TILE + 1):
            if not 0 <= tile_y < across:
                continue

            origin_x = tile_x * TILE
            origin_y = tile_y * TILE

            # Overlap of the kernel box with this tile, in world pixels.
            x_from = max(left, origin_x)
            x_to = min(left + size, origin_x + TILE)
            y_from = max(top, origin_y)
            y_to = min(top + size, origin_y + TILE)
            if x_from >= x_to or y_from >= y_to:
                continue

            target = tiles.get((tile_x, tile_y))
            if target is None:
                target = np.zeros((TILE, TILE), dtype=bool)
                tiles[(tile_x, tile_y)] = target

            target[
                y_from - origin_y : y_to - origin_y,
                x_from - origin_x : x_to - origin_x,
            ] |= kernel[
                y_from - top : y_to - top,
                x_from - left : x_to - left,
            ]


def resample(
    xs: np.ndarray, ys: np.ndarray, lats: np.ndarray, step_px: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Walk a projected polyline at a fixed pixel spacing.

    This is the linear interpolation that makes a brush stroke continuous: GPS
    fixes arrive every few seconds and can be tens of pixels apart, which would
    otherwise stamp a dotted line. The number of stamps depends on the length
    of the path, not on how densely it was sampled.
    """
    if len(xs) == 1:
        return xs, ys, lats

    steps = np.hypot(np.diff(xs), np.diff(ys))
    keep = np.concatenate([[True], steps > 0])
    xs, ys, lats = xs[keep], ys[keep], lats[keep]
    if len(xs) == 1:
        return xs, ys, lats

    travelled = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
    total = float(travelled[-1])

    count = max(int(total / step_px) + 1, 2)
    at = np.linspace(0.0, total, count)
    return (
        np.interp(at, travelled, xs),
        np.interp(at, travelled, ys),
        np.interp(at, travelled, lats),
    )


def stamp_path(
    lonlats: Sequence[tuple[float, float]],
    radius_m: float,
    zoom: int = geo.NATIVE_Z,
) -> Tiles:
    """Rasterise one path into tile masks at a zoom.

    Returns a boolean mask per touched tile. Radius is recomputed along the
    path rather than once for the whole track, because ground resolution
    changes with latitude. Defaults to the native z14 grid, which is what the
    blob store is built on; deeper zooms are used to render the sharp end of
    the PNG pyramid straight from geometry.
    """
    if not lonlats:
        return {}

    tiles: Tiles = {}

    for part in geo.split_antimeridian(list(lonlats)):
        projected = [geo.lonlat_to_px(lon, lat, zoom) for lon, lat in part]
        xs = np.array([p[0] for p in projected], dtype=np.float64)
        ys = np.array([p[1] for p in projected], dtype=np.float64)
        lats = np.array([geo.clamp_lat(lat) for _, lat in part], dtype=np.float64)

        mid_radius = geo.radius_px(radius_m, float(np.median(lats)), zoom)
        step = max(mid_radius * STEP_FRACTION, MIN_STEP_PX)

        xs, ys, lats = resample(xs, ys, lats, step)

        for x_px, y_px, lat in zip(xs, ys, lats):
            paint(
                tiles,
                x_px,
                y_px,
                _kernel_for(geo.radius_px(radius_m, float(lat), zoom)),
                zoom,
            )

    return tiles


def clip_runs(
    lonlats: Sequence[tuple[float, float]],
    radius_m: float,
    zoom: int,
    window: tuple[int, int, int, int],
) -> list[list[tuple[float, float]]]:
    """Split a path into the stretches that could touch a pixel window.

    stamp_path rasterises a whole track wherever it goes. That is right when
    the result is being stored, and badly wrong when the caller only wants one
    tile's worth: a morning run crossing ten tiles was being stamped end to end
    ten times over, and at z16 each pass resamples at four times the density.
    Clipping first makes the work proportional to what is actually wanted.

    Whole segments are kept, never split, so the stamped line still enters and
    leaves the window at the right angle. The window is padded by the brush,
    because a point outside it can still paint into it.
    """
    if len(lonlats) < 2:
        return [list(lonlats)] if lonlats else []

    left, top, right, bottom = window
    lats = [lat for _, lat in lonlats]
    pad = geo.radius_px(radius_m, max(abs(min(lats)), abs(max(lats))), zoom) + 2.0

    projected = [geo.lonlat_to_px(lon, lat, zoom) for lon, lat in lonlats]

    runs: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for index in range(len(lonlats) - 1):
        (ax, ay), (bx, by) = projected[index], projected[index + 1]
        touches = (
            min(ax, bx) <= right + pad
            and max(ax, bx) >= left - pad
            and min(ay, by) <= bottom + pad
            and max(ay, by) >= top - pad
        )
        if touches:
            if not current:
                current.append(lonlats[index])
            current.append(lonlats[index + 1])
        elif current:
            runs.append(current)
            current = []

    if current:
        runs.append(current)
    return runs


def read_blob(
    conn: sqlite3.Connection, kind: str, source: str, layer: str, x: int, y: int
) -> np.ndarray | None:
    row = conn.execute(
        "SELECT data FROM blobs WHERE kind = ? AND source = ? AND layer = ? "
        "AND x = ? AND y = ?",
        (kind, source, layer, x, y),
    ).fetchone()
    if row is None:
        return None
    return decode(row["data"], kind, source, layer, x, y)


def decode(
    data: bytes, kind: str, source: str, layer: str, x: int, y: int
) -> np.ndarray:
    expected = TILE * TILE
    if len(data) != expected:
        raise ValueError(
            f"Blob ({kind}, {source}, {layer}, {x}, {y}) holds {len(data)} bytes "
            f"but a {TILE}x{TILE} uint8 tile is {expected}. The blob store is "
            "corrupt. Delete it and run rebuild - the event log is the source "
            "of truth."
        )
    return np.frombuffer(data, dtype=np.uint8).reshape(TILE, TILE).copy()


def write_blob(
    conn: sqlite3.Connection,
    kind: str,
    source: str,
    layer: str,
    x: int,
    y: int,
    array: np.ndarray,
) -> None:
    if array.dtype != np.uint8 or array.shape != (TILE, TILE):
        raise ValueError(
            f"Blob ({kind}, {source}, {layer}, {x}, {y}) must be a "
            f"{TILE}x{TILE} uint8 array, got {array.shape} of {array.dtype}."
        )
    conn.execute(
        "INSERT INTO blobs (kind, source, layer, x, y, data) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(kind, source, layer, x, y) DO UPDATE SET data = excluded.data",
        (kind, source, layer, x, y, memoryview(array.tobytes())),
    )


def merge_mask(
    conn: sqlite3.Connection, kind: str, source: str, layer: str, tiles: Tiles
) -> None:
    """OR a set of tile masks into the stored fog or erase blobs."""
    for (x, y), mask in tiles.items():
        existing = read_blob(conn, kind, source, layer, x, y)
        merged = np.where(mask, MASK_ON, 0).astype(np.uint8)
        if existing is not None:
            merged = np.maximum(existing, merged)
        write_blob(conn, kind, source, layer, x, y, merged)


def clear_erase(conn: sqlite3.Connection, tiles: Tiles) -> None:
    """Take a painted footprint back out of every erase mask covering it.

    Erase is a composite-time subtract, so without this an erased patch of
    ground could never be drawn on again: the new fog went into the fog blob
    and the erase mask subtracted it straight back out at composite time, and
    the stroke simply did not appear.

    Invariant 2 says erased pixels stay erased across rebuilds and across
    re-imports of the same file, and they still do. Rebuild replays events in
    id order, so an erase drawn after a track still wins; re-importing a file
    is idempotent and creates no event, so nothing reaches here. What this
    changes is only the case the invariant does not cover - a deliberate new
    stroke over erased ground, which has to win or the eraser is a one-way
    door.
    """
    for (x, y), mask in tiles.items():
        rows = conn.execute(
            "SELECT source, layer, data FROM blobs WHERE kind = 'erase' "
            "AND x = ? AND y = ?",
            (x, y),
        ).fetchall()

        for row in rows:
            source, layer = row["source"], row["layer"]
            existing = decode(row["data"], "erase", source, layer, x, y)
            remaining = np.where(mask, 0, existing).astype(np.uint8)

            if remaining.any():
                write_blob(conn, "erase", source, layer, x, y, remaining)
            else:
                # An empty blob is not the same as no blob: it would be read,
                # decoded and unioned on every composite for ever.
                conn.execute(
                    "DELETE FROM blobs WHERE kind = 'erase' AND source = ? "
                    "AND layer = ? AND x = ? AND y = ?",
                    (source, layer, x, y),
                )


def merge_trail(
    conn: sqlite3.Connection, source: str, layer: str, tiles: Tiles
) -> None:
    """Add one pass to the trail count for every pixel this path covered.

    Counting is per event, not per stamped point, so the number recorded is
    how many separate tracks crossed a pixel. That is what makes a regular
    commute stand out from a one-off detour.
    """
    for (x, y), mask in tiles.items():
        existing = read_blob(conn, "trail", source, layer, x, y)
        if existing is None:
            existing = np.zeros((TILE, TILE), dtype=np.uint8)
        # Saturating add: a pixel crossed 300 times stays at 255.
        bumped = np.where(
            mask & (existing < 255), existing + np.uint8(1), existing
        ).astype(np.uint8)
        write_blob(conn, "trail", source, layer, x, y, bumped)


def geometry_points(geometry: str, event_id: int) -> list[tuple[float, float]]:
    """Read GeoJSON Point or LineString coordinates out of an event row."""
    try:
        parsed = json.loads(geometry)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Event {event_id} has geometry that is not valid JSON ({exc})."
        ) from exc

    kind = parsed.get("type")
    coordinates = parsed.get("coordinates")

    if kind == "Point":
        if not isinstance(coordinates, (list, tuple)) or len(coordinates) < 2:
            raise ValueError(
                f"Event {event_id} is a Point but its coordinates are "
                f"{coordinates!r} rather than a [lon, lat] pair."
            )
        return [(float(coordinates[0]), float(coordinates[1]))]

    if kind == "LineString":
        if not coordinates:
            raise ValueError(f"Event {event_id} is a LineString with no coordinates.")
        return [(float(pair[0]), float(pair[1])) for pair in coordinates]

    if kind == "Polygon":
        # The outer ring. Enough to bound the shape, which is all the callers
        # that reach here want; filling it goes through geometry_ring.
        return geometry_ring(geometry, event_id)

    raise ValueError(
        f"Event {event_id} has geometry type {kind!r}. Irfaran stores only "
        "Point, LineString and Polygon."
    )


def geometry_type(geometry: str, event_id: int) -> str:
    try:
        return str(json.loads(geometry).get("type"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Event {event_id} has geometry that is not valid JSON ({exc})."
        ) from exc


def geometry_ring(geometry: str, event_id: int) -> list[tuple[float, float]]:
    """The outer ring of a Polygon, closed.

    Holes are not read. An area someone drew round to say "I was all over
    here" does not have holes in it, and inventing support for them would mean
    inventing a way to draw them.
    """
    parsed = json.loads(geometry)
    rings = parsed.get("coordinates")

    if not isinstance(rings, (list, tuple)) or not rings:
        raise ValueError(f"Event {event_id} is a Polygon with no rings.")

    ring = rings[0]
    if not isinstance(ring, (list, tuple)) or len(ring) < 3:
        raise ValueError(
            f"Event {event_id} is a Polygon whose outer ring has "
            f"{len(ring) if hasattr(ring, '__len__') else 0} points. An area "
            "needs at least three."
        )

    points = [(float(pair[0]), float(pair[1])) for pair in ring]
    if points[0] != points[-1]:
        points.append(points[0])
    return points


def fill_ring(
    ring: Sequence[tuple[float, float]], zoom: int = geo.NATIVE_Z
) -> Tiles:
    """Rasterise the inside of a closed ring into tile masks.

    Drawn with Pillow rather than a scanline of our own: it is already a
    dependency, it fills consistently along shared edges, and a polygon fill
    written by hand is a well-known source of one-pixel seams between tiles.
    """
    if len(ring) < 3:
        return {}

    projected = [geo.lonlat_to_px(lon, lat, zoom) for lon, lat in ring]
    xs = [x for x, _ in projected]
    ys = [y for _, y in projected]

    span = geo.world_px(zoom)
    across = 2**zoom
    left = max(0, int(math.floor(min(xs))) // TILE)
    right = min(across - 1, int(math.floor(min(max(xs), span - 1))) // TILE)
    top = max(0, int(math.floor(min(ys))) // TILE)
    bottom = min(across - 1, int(math.floor(min(max(ys), span - 1))) // TILE)

    tiles: Tiles = {}
    for tile_x in range(left, right + 1):
        for tile_y in range(top, bottom + 1):
            origin_x, origin_y = tile_x * TILE, tile_y * TILE
            # Pillow clips whatever falls outside the canvas, so the ring is
            # handed over whole and only this tile's share comes back.
            local = [(x - origin_x, y - origin_y) for x, y in projected]

            canvas = Image.new("1", (TILE, TILE), 0)
            ImageDraw.Draw(canvas).polygon(local, fill=1, outline=1)
            mask = np.array(canvas, dtype=bool)
            if mask.any():
                tiles[(tile_x, tile_y)] = mask
    return tiles


def event_tile_bounds(event: sqlite3.Row) -> tuple[int, int, int, int] | None:
    """A cheap tile bounding box for an event, without rasterising it.

    Used to skip events that cannot possibly touch the tiles being rebuilt.
    Without it a targeted rebuild pays the full stamping cost for every event
    in the archive just to throw the result away, which is the difference
    between a live fix landing in milliseconds and in minutes.
    """
    try:
        points = geometry_points(event["geometry"], int(event["id"]))
    except ValueError:
        return None
    if not points:
        return None

    radius_m = float(event["radius_m"])
    lons = [lon for lon, _ in points]
    lats = [lat for _, lat in points]

    # An antimeridian-crossing track spans the world once projected, so no
    # box is worth trusting; let it through and stamp it properly.
    if max(lons) - min(lons) > 180.0:
        return None

    # Pad by the brush, in tiles, using the widest pixel scale in the span.
    pad_px = geo.radius_px(radius_m, max(abs(min(lats)), abs(max(lats)))) + 2.0
    west, north = geo.lonlat_to_px(min(lons), max(lats))
    east, south = geo.lonlat_to_px(max(lons), min(lats))

    return (
        int((west - pad_px) // TILE),
        int((north - pad_px) // TILE),
        int((east + pad_px) // TILE),
        int((south + pad_px) // TILE),
    )


def event_tiles(event: sqlite3.Row) -> set[tuple[int, int]]:
    """Which z14 tiles an event covers, without writing anything.

    Deleting an event needs this before the row goes away, so the tiles it
    dirtied can be rebuilt from what remains.
    """
    event_id = int(event["id"])
    return set(stamp_geometry(event["geometry"], float(event["radius_m"]), event_id))


# What an event does to the map.
#
#   add     clears fog and records a pass on the trail. Somebody went along
#           here, and the line down the middle says so.
#   reveal  clears fog and nothing else. Somebody was around here - which is
#           true of a childhood village or a week on an island, and drawing a
#           route through it would be inventing one.
#   erase   subtracts, at composite time. Invariant 2.
OPS = ("add", "reveal", "erase")

# Geometry an event may carry. Polygon is how an area reveal is stored: the
# ring is what was drawn, and filling it is the point.
GEOMETRIES = ("Point", "LineString", "Polygon")


def stamp_geometry(
    geometry: str, radius_m: float, event_id: int, zoom: int = geo.NATIVE_Z
) -> Tiles:
    """Rasterise an event's geometry, whatever shape it is.

    A Polygon is filled and its ring stamped with the brush, so the boundary
    is as round as a drawn one and a sliver too thin to enclose a pixel still
    reveals something. Everything else is a stamped path.
    """
    if geometry_type(geometry, event_id) == "Polygon":
        ring = geometry_ring(geometry, event_id)
        tiles = fill_ring(ring, zoom)
        for key, mask in stamp_path(ring, radius_m, zoom).items():
            if key in tiles:
                tiles[key] |= mask
            else:
                tiles[key] = mask
        return tiles

    return stamp_path(geometry_points(geometry, event_id), radius_m, zoom)


def stamp_event(
    conn: sqlite3.Connection,
    event: sqlite3.Row,
    restrict: set[tuple[int, int]] | None = None,
) -> set[tuple[int, int]]:
    """Rasterise a single event into the blob store.

    Returns the z14 tiles it touched, which is the rebuild scope for whatever
    is derived from them. `restrict` limits writes to a set of tiles, which is
    what makes a targeted rebuild after a delete possible without replaying
    the whole archive into the whole world.
    """
    event_id = int(event["id"])
    geometry = event["geometry"]
    radius_m = float(event["radius_m"])
    tiles = stamp_geometry(geometry, radius_m, event_id)

    if restrict is not None:
        tiles = {key: mask for key, mask in tiles.items() if key in restrict}
    if not tiles:
        return set()

    op = event["op"]
    source = event["source"]

    if op == "erase":
        # Erase ignores the layer it was drawn in, by design.
        merge_mask(conn, "erase", source, ERASE_LAYER, tiles)
    elif op in ("add", "reveal"):
        # Painting over erased ground takes the erase back off it. Without
        # this the stroke goes into the fog blob and is subtracted straight
        # out again when the tile is composed.
        clear_erase(conn, tiles)

        trail_tiles: Tiles = {}
        if op == "add":
            trail_radius_m = min(radius_m, trail_max_radius_m())
            if trail_radius_m >= radius_m:
                trail_tiles = tiles
            else:
                trail_tiles = stamp_geometry(geometry, trail_radius_m, event_id)
                if restrict is not None:
                    trail_tiles = {
                        key: mask
                        for key, mask in trail_tiles.items()
                        if key in restrict
                    }

        for layer in parse_layers(event["layers"], event_id):
            merge_mask(conn, "fog", source, layer, tiles)
            if trail_tiles:
                merge_trail(conn, source, layer, trail_tiles)
    else:
        raise ValueError(
            f"Event {event_id} has op {op!r}. Irfaran stores only "
            f"{', '.join(repr(name) for name in OPS)}."
        )

    return set(tiles.keys())


def parse_layers(layers: str, event_id: int) -> list[str]:
    try:
        parsed = json.loads(layers)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Event {event_id} has a layers column that is not valid JSON ({exc}). "
            'It must be a JSON array such as ["2024"].'
        ) from exc

    if not isinstance(parsed, list) or not parsed:
        raise ValueError(
            f"Event {event_id} has layers {parsed!r}. It must be a non-empty "
            'JSON array such as ["2024"] or ["prehistory"].'
        )
    return [str(layer) for layer in parsed]


def iter_events(conn: sqlite3.Connection) -> Iterator[sqlite3.Row]:
    """Every event in insertion order, which is what makes rebuild reproducible."""
    yield from conn.execute("SELECT * FROM events ORDER BY id")


def rebuild_tiles(
    conn: sqlite3.Connection, tiles: set[tuple[int, int]]
) -> set[tuple[int, int]]:
    """Rebuild a specific set of z14 tiles from the event log.

    Fog and trail accumulate, so removing an event cannot be undone by
    subtracting it - the tiles it touched have to be built again from whatever
    events remain. Restricting the replay to those tiles is what keeps undo
    instant instead of a full archive rebuild.
    """
    if not tiles:
        return set()

    placeholders = ",".join("(?, ?)" for _ in tiles)
    params: list[int] = []
    for tile_x, tile_y in tiles:
        params.extend((tile_x, tile_y))
    conn.execute(f"DELETE FROM blobs WHERE (x, y) IN ({placeholders})", params)

    min_x = min(x for x, _ in tiles)
    max_x = max(x for x, _ in tiles)
    min_y = min(y for _, y in tiles)
    max_y = max(y for _, y in tiles)

    touched: set[tuple[int, int]] = set()
    for event in iter_events(conn):
        bounds = event_tile_bounds(event)
        if bounds is not None:
            west, north, east, south = bounds
            if east < min_x or west > max_x or south < min_y or north > max_y:
                continue
        touched |= stamp_event(conn, event, restrict=tiles)
    return touched


def rebuild(
    conn: sqlite3.Connection, events: Iterable[sqlite3.Row] | None = None
) -> tuple[int, set[tuple[int, int]]]:
    """Drop every blob and replay the event log.

    This is the guarantee behind invariant 1. Deleting the derived caches and
    running this must produce byte-identical output, because the only inputs
    are the events and the order they are replayed in.
    """
    conn.execute("DELETE FROM blobs")

    touched: set[tuple[int, int]] = set()
    count = 0
    for event in events if events is not None else iter_events(conn):
        touched |= stamp_event(conn, event)
        count += 1
    return count, touched
