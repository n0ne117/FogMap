# SPDX-License-Identifier: AGPL-3.0-or-later
"""View composition and downsampling.

Section 6 of the build plan, in one place:

    fog_add   = OR  over selected (kind=fog,   source, layer) blobs
    fog_erase = OR  over selected (kind=erase, source, layer) blobs
    fog       = fog_add AND NOT fog_erase

    trail_sum = SUM over selected (kind=trail, source, layer) blobs, saturating
    trail     = trail_sum masked by NOT fog_erase

Erase is a subtract mask applied at composite time, never a bit cleared in the
fog blob. That is invariant 2, and it is what makes an erase survive both a
full rebuild and a re-import of the file that drew the fog underneath it.
"""

from __future__ import annotations

import io
import math
import multiprocessing
import os
import shutil
import sqlite3
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image, ImageFilter

from irfaran import db, geo, raster, settings_env

TILE = geo.TILE_PX
HALF = TILE // 2

VIEW_ALL = "all"
VIEW_PREHISTORY = "prehistory"
YEAR_PREFIX = "year:"

THEMES = ("light", "dark")
KINDS = ("fog", "trail")

# Render jobs below which everything stays in this process. A pool costs a
# couple of hundred milliseconds to start, which is most of a single stroke.
PARALLEL_FROM = 4

# How far the fog fades out from explored ground. The build plan calls edge
# softening the difference between "a map" and Fog of World, and warns that
# tuning it is an evening on its own - hence the environment override.
DEFAULT_FOG_EDGE_PX = 2.5

# Fog is drawn where the ground is UNEXPLORED, so this is the colour of the
# unknown.
# A dark neutral grey rather than near-black. Over a dark basemap, near-black
# fog and the ground beneath it differ by so little that thinning the fog
# reveals almost nothing; a grey reads as haze over the map instead of as an
# absence of one.
# A neutral mid grey for the dark theme: fog is the absence of knowledge
# rather than a feature of the landscape, so it does not want to belong to the
# palette underneath it. The light theme keeps its pale fog, because the two
# themes have to stay distinguishable - and anyone who prefers the grey there
# can set it, which is a stored setting rather than a rebuild.
FOG_COLOUR = {
    "dark": (94, 92, 100),
    "light": (232, 232, 228),
}

# How solid the baked fog is, 0 to 255.
#
# Fully opaque, as section 8 says. How much of the map shows through is a
# viewing choice rather than a property of the data, so it is made in the
# browser with MapLibre's raster-opacity - which costs nothing, applies
# instantly and needs no re-render. Baking it in here would fix one answer
# into every tile and still be wrong for the other theme.
DEFAULT_FOG_ALPHA = 255


SETTING_FOG_COLOUR = "fog_colour_{theme}"


def parse_colour(raw: str, where: str) -> tuple[int, int, int]:
    """Read #rrggbb, or #rgb, into a tuple. Loud about anything else."""
    text = raw.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(
            f"{where} must be a hex colour like #1c1e23, got {raw!r}."
        )
    try:
        return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        raise ValueError(
            f"{where} must be a hex colour like #1c1e23, got {raw!r}."
        ) from None


def to_hex(colour: tuple[int, int, int]) -> str:
    return "#%02x%02x%02x" % colour


def fog_colour(theme: str, conn: sqlite3.Connection | None = None) -> tuple[int, int, int]:
    """The colour of the unknown, for a theme.

    Order is environment, then the stored setting, then the built-in. Unlike
    fog opacity - which is a viewing choice the browser makes for free - the
    colour is baked into the tiles, so changing it means a re-render. That is
    the price of the tile endpoint being a file read and nothing else.
    """
    if theme not in FOG_COLOUR:
        raise ValueError(
            f"Unknown theme {theme!r}. Irfaran renders {' and '.join(THEMES)}."
        )

    name = f"IRFARAN_FOG_COLOUR_{theme.upper()}"
    from_env = settings_env.get(f"FOG_COLOUR_{theme.upper()}")
    if from_env:
        return parse_colour(from_env, name)

    if conn is not None:
        key = SETTING_FOG_COLOUR.format(theme=theme)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        if row and str(row["value"]).strip():
            return parse_colour(str(row["value"]), f"Setting {key}")

    return FOG_COLOUR[theme]


def fog_alpha() -> int:
    raw = settings_env.get("FOG_ALPHA")
    if not raw:
        return DEFAULT_FOG_ALPHA
    try:
        value = int(float(raw))
    except ValueError:
        raise ValueError(
            f"IRFARAN_FOG_ALPHA must be a number from 0 to 255, got {raw!r}. "
            f"Unset it to use the default of {DEFAULT_FOG_ALPHA}."
        ) from None
    return max(0, min(255, value))


SETTING_TRAIL_RAMP = "trail_ramp"

# Pass count to colour. Counts are heavily skewed - most pixels are crossed
# once - so the ramp is walked on a log scale, otherwise every trail but the
# daily commute renders as the same dim first step.
#
# Each ramp has a dark and a light variant, because a colour that glows over a
# dark basemap disappears over a pale one.
TRAIL_RAMPS = {
    "dark": [
        (0.00, (90, 30, 90, 0)),
        (0.01, (120, 40, 110, 205)),
        (0.35, (200, 60, 80, 230)),
        (0.65, (240, 130, 50, 242)),
        (0.85, (250, 200, 90, 250)),
        (1.00, (255, 252, 220, 255)),
    ],
    "light": [
        (0.00, (60, 20, 90, 0)),
        (0.01, (95, 35, 130, 195)),
        (0.35, (170, 45, 95, 220)),
        (0.65, (215, 110, 40, 238)),
        (0.85, (235, 165, 40, 248)),
        (1.00, (140, 60, 0, 255)),
    ],
}

TRAIL_RAMP_SETS: dict[str, dict[str, list]] = {
    "ember": TRAIL_RAMPS,
    "ice": {
        "dark": [
            (0.00, (10, 40, 80, 0)),
            (0.01, (20, 70, 130, 205)),
            (0.35, (30, 130, 190, 230)),
            (0.65, (60, 190, 220, 242)),
            (0.85, (150, 230, 240, 250)),
            (1.00, (235, 252, 255, 255)),
        ],
        "light": [
            (0.00, (10, 30, 70, 0)),
            (0.01, (25, 60, 130, 195)),
            (0.35, (20, 105, 170, 220)),
            (0.65, (25, 150, 180, 238)),
            (0.85, (40, 175, 190, 248)),
            (1.00, (10, 70, 100, 255)),
        ],
    },
    "moss": {
        "dark": [
            (0.00, (20, 50, 30, 0)),
            (0.01, (35, 85, 55, 205)),
            (0.35, (70, 140, 70, 230)),
            (0.65, (140, 190, 70, 242)),
            (0.85, (210, 230, 110, 250)),
            (1.00, (245, 255, 210, 255)),
        ],
        "light": [
            (0.00, (20, 50, 30, 0)),
            (0.01, (40, 95, 55, 195)),
            (0.35, (70, 135, 55, 220)),
            (0.65, (120, 165, 40, 238)),
            (0.85, (165, 185, 40, 248)),
            (1.00, (60, 85, 10, 255)),
        ],
    },
    "mono": {
        "dark": [
            (0.00, (255, 255, 255, 0)),
            (0.01, (200, 205, 215, 190)),
            (0.50, (235, 238, 245, 226)),
            (1.00, (255, 255, 255, 255)),
        ],
        "light": [
            (0.00, (20, 22, 28, 0)),
            (0.01, (90, 95, 105, 190)),
            (0.50, (50, 54, 62, 226)),
            (1.00, (10, 12, 16, 255)),
        ],
    },
}


def trail_ramp(conn: sqlite3.Connection | None = None) -> str:
    """Which colour ramp the trails are drawn with.

    Environment, then the stored setting, then ember - the one section 8
    picked. Like the fog colour, this is baked into the tiles, so changing it
    costs a render.
    """
    from_env = settings_env.get("TRAIL_RAMP").lower()
    if from_env:
        return check_ramp(from_env)

    if conn is not None:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (SETTING_TRAIL_RAMP,)
        ).fetchone()
        if row and str(row["value"]).strip():
            return check_ramp(str(row["value"]))

    return "ember"


def check_ramp(name: str) -> str:
    cleaned = name.strip().lower()
    if cleaned not in TRAIL_RAMP_SETS:
        raise ValueError(
            f"Unknown trail colours {name!r}. Irfaran has "
            f"{', '.join(sorted(TRAIL_RAMP_SETS))}."
        )
    return cleaned


def view_layers(view: str) -> set[str] | None:
    """Layers a canonical view selects, or None meaning every layer.

    The canonical views are the only ones that exist. There is deliberately no
    general filter UI, so this is a closed set.
    """
    if view == VIEW_ALL:
        return None
    if view == VIEW_PREHISTORY:
        return {VIEW_PREHISTORY}
    if view.startswith(YEAR_PREFIX):
        year = view[len(YEAR_PREFIX) :]
        if not (len(year) == 4 and year.isdigit()):
            raise ValueError(
                f"View {view!r} is not a valid year view. Expected "
                "'year:YYYY' with a four digit year."
            )
        return {year}
    raise ValueError(
        f"Unknown view {view!r}. Valid views are 'all', 'prehistory' and "
        "'year:YYYY'."
    )


def available_views(conn: sqlite3.Connection) -> list[str]:
    """Canonical views that have pixels behind them right now."""
    rows = conn.execute(
        "SELECT DISTINCT layer FROM blobs WHERE kind IN ('fog', 'trail') "
        "ORDER BY layer"
    ).fetchall()
    layers = [row["layer"] for row in rows]

    views = [VIEW_ALL]
    views += [f"{YEAR_PREFIX}{layer}" for layer in layers if layer.isdigit()]
    if VIEW_PREHISTORY in layers:
        views.append(VIEW_PREHISTORY)
    return views


def views_touching(
    conn: sqlite3.Connection, tiles: set[tuple[int, int]]
) -> list[str]:
    """Canonical views with pixels inside a set of z14 tiles.

    An erase is subtracted from every view, so every view has to be
    re-rendered - but a year nobody travelled in the tiles the erase covers
    renders back to exactly the bytes already on disk. With a view per year
    that is the difference between an erase costing four seconds and half a
    second, and it changes nothing about what ends up on disk.
    """
    if not tiles:
        return []

    clause = " OR ".join(["(x = ? AND y = ?)"] * len(tiles))
    params: list[object] = [value for tile in sorted(tiles) for value in tile]
    rows = conn.execute(
        f"SELECT DISTINCT layer FROM blobs WHERE kind IN ('fog', 'trail') "
        f"AND ({clause})",
        params,
    ).fetchall()
    layers = {row["layer"] for row in rows}

    views = [VIEW_ALL]
    views += [f"{YEAR_PREFIX}{layer}" for layer in sorted(layers) if layer.isdigit()]
    if VIEW_PREHISTORY in layers:
        views.append(VIEW_PREHISTORY)
    return views


def _blobs(
    conn: sqlite3.Connection, kind: str, x: int, y: int, layers: set[str] | None
) -> list[np.ndarray]:
    sql = "SELECT source, layer, data FROM blobs WHERE kind = ? AND x = ? AND y = ?"
    params: list[object] = [kind, x, y]
    if layers is not None:
        sql += f" AND layer IN ({','.join('?' * len(layers))})"
        params.extend(sorted(layers))
    sql += " ORDER BY source, layer"

    return [
        raster.decode(row["data"], kind, row["source"], row["layer"], x, y)
        for row in conn.execute(sql, params)
    ]


def erase_mask(conn: sqlite3.Connection, x: int, y: int) -> np.ndarray:
    """Union of every erase blob covering a tile.

    Erase deliberately ignores the layer filter. A stroke that removes fog
    wrongly cleared by GPS drift is correcting the record for every year, not
    only the year it happened to be drawn in.
    """
    combined = np.zeros((TILE, TILE), dtype=bool)
    for blob in _blobs(conn, "erase", x, y, None):
        combined |= blob > 0
    return combined


def composite_tile(
    conn: sqlite3.Connection, view: str, x: int, y: int
) -> tuple[np.ndarray, np.ndarray]:
    """Compose one z14 tile for a view.

    Returns (fog, trail): a boolean explored mask and a uint8 pass count.
    """
    layers = view_layers(view)

    fog = np.zeros((TILE, TILE), dtype=bool)
    for blob in _blobs(conn, "fog", x, y, layers):
        fog |= blob > 0

    trail = np.zeros((TILE, TILE), dtype=np.uint16)
    for blob in _blobs(conn, "trail", x, y, layers):
        trail += blob

    erased = erase_mask(conn, x, y)
    fog &= ~erased
    trail[erased] = 0

    return fog, np.clip(trail, 0, 255).astype(np.uint8)


def deep_tiles(
    conn: sqlite3.Connection, view: str, parent: tuple[int, int], zoom: int
) -> dict[tuple[int, int], tuple[np.ndarray, np.ndarray]]:
    """Compose every tile at `zoom` inside one native tile, from the event log.

    Below z14 the pyramid is built by folding the blob store upwards. Above it
    there is nothing to fold: the blobs hold two-and-a-bit pixels per brush and
    upscaling them is what made a hand-drawn stroke arrive as a smear. So the
    deep levels are stamped from the same geometry at their own resolution.

    Nothing new is stored. The event log is still the only source of truth
    (invariant 1), the composite rules are the ones at the top of this file,
    and it all still happens at ingest rather than in a tile request
    (invariant 3). Only the grid the stamping lands on is different.
    """
    layers = view_layers(view)
    wanted = set(geo.descendants(*parent, zoom))

    # The parent's footprint in this zoom's pixels. Paths are clipped to it
    # before stamping, so a track that crosses the whole city is not rasterised
    # end to end once per tile it passes through.
    across = TILE * 2 ** (zoom - geo.NATIVE_Z)
    window = (
        parent[0] * across,
        parent[1] * across,
        (parent[0] + 1) * across,
        (parent[1] + 1) * across,
    )

    def keep(source: raster.Tiles, out: raster.Tiles) -> raster.Tiles:
        for key, mask in source.items():
            if key not in wanted:
                continue
            target = out.get(key)
            if target is None:
                out[key] = mask.copy()
            else:
                target |= mask
        return out

    def stamp(geometry: str, radius_m: float, event_id: int) -> raster.Tiles:
        out: raster.Tiles = {}

        if raster.geometry_type(geometry, event_id) == "Polygon":
            # An area is filled, so there is nothing to clip into runs - the
            # fill already only produces tiles the ring encloses.
            return keep(raster.stamp_geometry(geometry, radius_m, event_id, zoom), out)

        points = raster.geometry_points(geometry, event_id)
        for run in raster.clip_runs(points, radius_m, zoom, window):
            keep(raster.stamp_path(run, radius_m, zoom), out)
        return out

    fog_add: dict[tuple[int, int], np.ndarray] = {}
    erased: dict[tuple[int, int], np.ndarray] = {}
    trails: dict[tuple[int, int], np.ndarray] = {}

    for event in raster.iter_events(conn):
        bounds = raster.event_tile_bounds(event)
        if bounds is not None:
            west, north, east, south = bounds
            if not (west <= parent[0] <= east and north <= parent[1] <= south):
                continue

        event_id = int(event["id"])
        op = event["op"]
        event_layers = raster.parse_layers(event["layers"], event_id)

        # Erase ignores the layer filter, exactly as it does at z14.
        if op != "erase" and layers is not None and not (layers & set(event_layers)):
            continue

        geometry = event["geometry"]
        radius_m = float(event["radius_m"])
        stamped = stamp(geometry, radius_m, event_id)
        if not stamped:
            continue

        if op == "erase":
            for key, mask in stamped.items():
                target = erased.setdefault(key, np.zeros((TILE, TILE), dtype=bool))
                target |= mask
            continue

        # An add that lands on erased ground lifts the erase, in id order,
        # which is what makes redrawing there work at z14 too.
        for key, mask in stamped.items():
            if key in erased:
                erased[key] &= ~mask
            target = fog_add.setdefault(key, np.zeros((TILE, TILE), dtype=bool))
            target |= mask

        # A reveal clears fog and leaves no track, so it contributes nothing
        # here. That is the whole difference between the two.
        if op != "add":
            continue

        trail_radius_m = min(radius_m, raster.trail_max_radius_m())
        trail_stamped = (
            stamped
            if trail_radius_m >= radius_m
            else stamp(geometry, trail_radius_m, event_id)
        )
        # One pass per event per layer, saturating - the same counting rule as
        # merge_trail, so the colour ramp means the same thing at every zoom.
        bump = len(event_layers) if layers is None else len(layers & set(event_layers))
        for key, mask in trail_stamped.items():
            counts = trails.setdefault(key, np.zeros((TILE, TILE), dtype=np.uint16))
            counts[mask] += bump

    out: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]] = {}
    for key in set(fog_add) | set(trails):
        fog = fog_add.get(key, np.zeros((TILE, TILE), dtype=bool)).copy()
        trail = trails.get(key, np.zeros((TILE, TILE), dtype=np.uint16)).copy()
        gone = erased.get(key)
        if gone is not None:
            fog &= ~gone
            trail[gone] = 0
        if fog.any() or trail.any():
            out[key] = (fog, np.clip(trail, 0, 255).astype(np.uint8))
    return out


def fold_up(
    tiles: dict[tuple[int, int], tuple[np.ndarray, np.ndarray]],
) -> dict[tuple[int, int], tuple[np.ndarray, np.ndarray]]:
    """Combine a level of composed tiles into the level above it.

    The same 2x2 rule the rest of the pyramid uses: max for fog so a thin
    trail survives, sum for the pass count.
    """
    grouped: dict[tuple[int, int], dict[tuple[int, int], np.ndarray]] = {}
    trails: dict[tuple[int, int], dict[tuple[int, int], np.ndarray]] = {}

    for (x, y), (fog, trail) in tiles.items():
        parent = (x // 2, y // 2)
        offset = (x % 2, y % 2)
        grouped.setdefault(parent, {})[offset] = fog
        trails.setdefault(parent, {})[offset] = trail

    return {
        parent: (
            parent_tile(children, "max"),
            parent_tile(trails[parent], "sum"),
        )
        for parent, children in grouped.items()
    }


def downsample_max(array: np.ndarray) -> np.ndarray:
    """2x2 max, halving a tile. Used for fog and erase masks.

    Max rather than mean, so a thin explored trail survives all the way to z0
    instead of fading out two zoom levels down.
    """
    return array.reshape(HALF, 2, HALF, 2).max(axis=(1, 3))


def downsample_sum(array: np.ndarray) -> np.ndarray:
    """2x2 sum saturating at 255, halving a tile. Used for trail counts."""
    summed = array.astype(np.uint16).reshape(HALF, 2, HALF, 2).sum(axis=(1, 3))
    return np.clip(summed, 0, 255).astype(np.uint8)


def parent_tile(
    children: dict[tuple[int, int], np.ndarray], how: str
) -> np.ndarray:
    """Assemble one parent tile from up to four children.

    Children are keyed by their position within the parent, (0,0) being the
    north-west quadrant. Missing children are empty, which is what an
    unexplored quadrant is.
    """
    if how == "max":
        shrink, dtype = downsample_max, None
    elif how == "sum":
        shrink, dtype = downsample_sum, np.uint8
    else:
        raise ValueError(
            f"Downsampling must be 'max' for fog and erase or 'sum' for trail, "
            f"got {how!r}."
        )

    sample = next(iter(children.values()), None)
    out_dtype = dtype or (sample.dtype if sample is not None else bool)
    parent = np.zeros((TILE, TILE), dtype=out_dtype)

    for (quadrant_x, quadrant_y), child in children.items():
        if quadrant_x not in (0, 1) or quadrant_y not in (0, 1):
            raise ValueError(
                f"Child quadrant ({quadrant_x}, {quadrant_y}) is outside the "
                "2x2 parent. Quadrants are 0 or 1 on each axis."
            )
        parent[
            quadrant_y * HALF : (quadrant_y + 1) * HALF,
            quadrant_x * HALF : (quadrant_x + 1) * HALF,
        ] = shrink(child)

    return parent


def trail_lut(theme: str, ramp: str = "ember") -> np.ndarray:
    """A 256-entry pass-count to RGBA lookup table.

    Counts are mapped through log1p before the ramp is walked, so the
    difference between one pass and four is visible rather than being crushed
    into the bottom of a linear scale.
    """
    try:
        anchors = TRAIL_RAMP_SETS[check_ramp(ramp)][theme]
    except KeyError:
        raise ValueError(
            f"Unknown theme {theme!r}. Irfaran renders {' and '.join(THEMES)}."
        ) from None

    counts = np.arange(256, dtype=np.float64)
    position = np.log1p(counts) / math.log1p(255.0)

    stops = np.array([stop for stop, _ in anchors])
    lut = np.zeros((256, 4), dtype=np.uint8)
    for channel in range(4):
        values = np.array([colour[channel] for _, colour in anchors], dtype=np.float64)
        lut[:, channel] = np.interp(position, stops, values).round().astype(np.uint8)

    lut[0] = (0, 0, 0, 0)  # never crossed, never drawn
    return lut


def fog_edge_px() -> float:
    """How far the fog fades out from explored ground, in pixels."""
    raw = settings_env.get("FOG_EDGE_PX")
    if not raw:
        return DEFAULT_FOG_EDGE_PX
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(
            f"IRFARAN_FOG_EDGE_PX must be a number, got {raw!r}. Unset it to "
            f"use the default of {DEFAULT_FOG_EDGE_PX} px, or set 0 for a hard "
            "edge."
        ) from None


def soften(explored: np.ndarray, radius: float) -> np.ndarray:
    """Fade the fog outwards from explored ground.

    The fade only ever runs into the fog: explored pixels stay completely
    clear. Blurring the mask itself would leave a one-pixel trail half hidden
    under its own haze, which is worse than a hard edge, not better.
    """
    if radius <= 0.0 or not explored.any():
        return np.where(explored, 0.0, 1.0).astype(np.float32)

    blurred = Image.fromarray(
        np.where(explored, 255, 0).astype(np.uint8), mode="L"
    ).filter(ImageFilter.GaussianBlur(radius))

    opacity = 1.0 - np.asarray(blurred, dtype=np.float32) / 255.0
    opacity[explored] = 0.0
    return opacity


def render_fog(
    fog: np.ndarray,
    theme: str,
    edge_px: float | None = None,
    alpha: int | None = None,
    colour: tuple[int, int, int] | None = None,
) -> np.ndarray:
    """Paint the unexplored ground, leaving explored ground transparent."""
    if theme not in FOG_COLOUR:
        raise ValueError(
            f"Unknown theme {theme!r}. Irfaran renders {' and '.join(THEMES)}."
        )
    if colour is None:
        colour = FOG_COLOUR[theme]

    radius = fog_edge_px() if edge_px is None else edge_px
    solid = fog_alpha() if alpha is None else alpha
    opacity = soften(fog, radius)

    rgba = np.empty((TILE, TILE, 4), dtype=np.uint8)
    rgba[..., 0] = colour[0]
    rgba[..., 1] = colour[1]
    rgba[..., 2] = colour[2]
    rgba[..., 3] = np.clip(opacity * solid, 0, 255).astype(np.uint8)
    return rgba


# How far a trail is feathered at the deep zoom levels, in pixels.
#
# On the native grid a track is one pixel and softening it would erase it. Two
# levels down it is four or five pixels of hard-edged stripe with visibly
# stepped diagonals, which is the difference between a heat map and a bar
# chart. Feathering the counts before the ramp is walked turns it back into
# something that reads as heat.
DEEP_TRAIL_SOFT_PX = 0.7

# How much the trail is thickened when a tile is zoomed out, by zoom.
#
# Folding the pyramid upwards keeps a track one pixel wide however far out you
# go, so at z7 a whole year of running is a few hundred lit pixels scattered
# over nine tiles - individually at full brightness, and collectively
# invisible. Measured on a real archive: 454 lit pixels out of 590,000.
#
# Thickening rather than brightening, because brightness was never the
# problem. A dilation also keeps the counts exactly as they are, where a blur
# would spread them and dim the middle of every line.
#
# Applied when the tile is drawn, never to the array handed to the level
# above, or each level would thicken what the last one already had.
TRAIL_GROW_PX = {11: 3, 10: 3, 9: 5, 8: 5, 7: 5, 6: 5, 5: 5, 4: 3, 3: 3}


def trail_grow(zoom: int) -> int:
    return TRAIL_GROW_PX.get(zoom, 0)


def render_trail(
    trail: np.ndarray,
    theme: str,
    ramp: str = "ember",
    soft_px: float = 0.0,
    grow_px: int = 0,
) -> np.ndarray:
    if grow_px > 2 and trail.any():
        # Each pixel takes the brightest value near it, so a hairline becomes
        # a line you can actually see without any count changing.
        trail = np.asarray(
            Image.fromarray(trail, mode="L").filter(ImageFilter.MaxFilter(grow_px)),
            dtype=np.uint8,
        )

    if soft_px > 0 and trail.any():
        spread = np.asarray(
            Image.fromarray(trail, mode="L").filter(
                ImageFilter.GaussianBlur(radius=soft_px)
            ),
            dtype=np.uint8,
        )
        # Never dimmer than it was: the blur may only add glow around a track,
        # not take brightness off the middle of one.
        trail = np.maximum(trail, spread)
    return trail_lut(theme, ramp)[trail]


def encode_png(rgba: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def tile_path(
    root: Path, theme: str, view: str, kind: str, zoom: int, x: int, y: int
) -> Path:
    """Where a rendered tile lives on disk.

    The view name goes in a path segment, so `year:2024` becomes `year-2024` -
    a colon in a path works on Linux but breaks the moment anything touches it
    from Windows or a URL that has not been escaped.
    """
    return root / theme / view.replace(":", "-") / kind / str(zoom) / str(x) / f"{y}.png"


def placeholder_tile(
    theme: str, kind: str, conn: sqlite3.Connection | None = None
) -> bytes:
    """The tile served where nothing has been rendered.

    Fog covers the whole world, so a tile with no data is not missing - it is
    entirely unexplored, and must come back as solid fog. Returning 404 here
    would punch a hole in the fog over every part of the world the user has
    never been, which is most of it.
    """
    if kind == "fog":
        return encode_png(
            render_fog(
                np.zeros((TILE, TILE), dtype=bool),
                theme,
                colour=fog_colour(theme, conn),
            )
        )
    return encode_png(
        render_trail(np.zeros((TILE, TILE), dtype=np.uint8), theme, trail_ramp(conn))
    )


def write_placeholders(
    root: Path, conn: sqlite3.Connection | None = None
) -> list[Path]:
    written = []
    for theme in THEMES:
        for kind in KINDS:
            destination = root / theme / f"empty-{kind}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(placeholder_tile(theme, kind, conn))
            written.append(destination)
    return written


def tiles_with_data(conn: sqlite3.Connection, layers: set[str] | None) -> set[tuple[int, int]]:
    """z14 tiles holding fog or trail for the given layers."""
    sql = "SELECT DISTINCT x, y FROM blobs WHERE kind IN ('fog', 'trail')"
    params: list[object] = []
    if layers is not None:
        sql += f" AND layer IN ({','.join('?' * len(layers))})"
        params.extend(sorted(layers))
    return {(int(row["x"]), int(row["y"])) for row in conn.execute(sql, params)}


def pyramid_levels(
    native: set[tuple[int, int]],
) -> dict[int, set[tuple[int, int]]]:
    """Which tiles exist at every zoom, folding z14 upwards.

    A tile exists at a zoom if anything beneath it does. This is what lets the
    render walk only the populated part of the world instead of 2**28 tiles.
    """
    levels = {geo.NATIVE_Z: set(native)}
    for zoom in range(geo.NATIVE_Z - 1, -1, -1):
        levels[zoom] = {(x // 2, y // 2) for x, y in levels[zoom + 1]}
    return levels


def prune_stale(root: Path, view: str, keep: set[Path]) -> int:
    """Delete rendered tiles for a view that this render did not write.

    Rendering only writes tiles that have data behind them. Without this, the
    last event in an area could be deleted and its tiles would stay on disk
    forever - the endpoint would keep serving them and the deletion would look
    like it had silently failed.
    """
    removed = 0
    for theme in THEMES:
        directory = root / theme / view.replace(":", "-")
        if not directory.is_dir():
            continue

        for existing in directory.rglob("*.png"):
            if existing not in keep:
                existing.unlink(missing_ok=True)
                removed += 1

        # Deepest first, so a column that has just been emptied goes with the
        # tiles that were in it rather than accumulating over years of editing.
        for leftover in sorted(directory.rglob("*"), key=lambda p: -len(p.parts)):
            if leftover.is_dir() and not any(leftover.iterdir()):
                leftover.rmdir()
    return removed


def render_deep(
    conn: sqlite3.Connection,
    root: Path,
    view: str,
    parent: tuple[int, int],
    themes: tuple[str, ...],
    scope: dict[int, set[tuple[int, int]]] | None,
    colours: dict[str, tuple[int, int, int]],
    ramp: str = "ember",
    kinds: tuple[str, ...] = KINDS,
) -> tuple[int, set[Path]]:
    """Stamp the deep levels under one native tile, straight from geometry.

    Only the deepest level is stamped. The levels between it and z14 are folded
    up from it the same way z13 is folded up from z14, which is both consistent
    with the rest of the pyramid and cheaper than replaying the event log once
    per level.

    Returns (tiles written, every path that legitimately holds data), the
    second so the pruning pass afterwards knows what not to delete.
    """
    written = 0
    kept: set[Path] = set()

    composed = deep_tiles(conn, view, parent, geo.MAX_Z)
    for zoom in range(geo.MAX_Z, geo.NATIVE_Z, -1):
        if zoom < geo.MAX_Z:
            composed = fold_up(composed)

        for (x, y), (fog, trail) in composed.items():
            wanted = scope is None or (x, y) in scope.get(zoom, frozenset())
            for theme in themes:
                for kind in kinds:
                    destination = tile_path(root, theme, view, kind, zoom, x, y)
                    kept.add(destination)
                    if not wanted:
                        continue
                    rgba = (
                        render_fog(fog, theme, colour=colours[theme])
                        if kind == "fog"
                        else render_trail(trail, theme, ramp)
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(encode_png(rgba))
                    written += 1

    return written, kept


Job = tuple[
    str,  # "shallow" or "deep"
    str,  # database path
    str,  # tiles root
    str,  # view
    tuple[str, ...],  # themes
    "dict[int, set[tuple[int, int]]] | None",  # scope
    "tuple[int, int] | None",  # native tile, for deep jobs
    tuple[str, ...],  # kinds
]


def _render_job(job: Job) -> tuple[int, list[str]]:
    """One unit of rendering, in a worker process.

    Module level and taking a single tuple, because that is what a process
    pool can pickle. A connection cannot cross a process boundary, so each
    worker opens its own; they only read.
    """
    kind, db_path, root, view, themes, scope, parent, kinds = job
    conn = db.connect(db_path)
    try:
        if kind == "shallow":
            written, kept, _ = render_shallow(
                conn, Path(root), view, themes, scope, kinds
            )
        else:
            colours = {theme: fog_colour(theme, conn) for theme in themes}
            written, kept = render_deep(
                conn,
                Path(root),
                view,
                parent,
                themes,
                scope,
                colours,
                trail_ramp(conn),
                kinds,
            )
        return written, [str(path) for path in kept]
    finally:
        conn.close()


def render_shallow(
    conn: sqlite3.Connection,
    root: Path,
    view: str,
    themes: tuple[str, ...],
    scope: dict[int, set[tuple[int, int]]] | None,
    kinds: tuple[str, ...] = KINDS,
) -> tuple[int, set[Path], list[tuple[int, int]]]:
    """z0 to z14 for one view, folded up from the blob store.

    Returns (tiles written, paths that hold data, native tiles whose deep
    levels still need stamping). Cheap next to the deep levels - a hundred
    native tiles compose in well under a second - so this stays in-process and
    the fan-out happens afterwards.

    `scope` limits which tiles are encoded and written, and is what section 6's
    rebuild scope buys: one edit touches one z14 tile and its fourteen
    ancestors, so encoding the other few hundred tiles of a view produces
    byte-identical files nobody asked for. The walk itself still covers the
    whole view - a parent is the maximum of its children, so its array cannot
    be built without them - but composing arrays is cheap and encoding PNGs is
    not. Tiles outside the scope keep their files: they are counted as kept so
    the pruning pass leaves them alone.
    """
    native = tiles_with_data(conn, view_layers(view))
    if not native:
        # Everything in this view is gone. Its tiles have to go with it.
        prune_stale(root, view, set())
        return 0, set(), []

    levels = pyramid_levels(native)
    written = 0
    kept: set[Path] = set()
    colours = {theme: fog_colour(theme, conn) for theme in themes}
    ramp = trail_ramp(conn)

    def in_scope(zoom: int, x: int, y: int) -> bool:
        return scope is None or (x, y) in scope.get(zoom, frozenset())

    def build(zoom: int, x: int, y: int) -> tuple[np.ndarray, np.ndarray] | None:
        nonlocal written

        if zoom == geo.NATIVE_Z:
            fog, trail = composite_tile(conn, view, x, y)
        else:
            fog_children: dict[tuple[int, int], np.ndarray] = {}
            trail_children: dict[tuple[int, int], np.ndarray] = {}
            for offset_x in (0, 1):
                for offset_y in (0, 1):
                    child_x, child_y = x * 2 + offset_x, y * 2 + offset_y
                    if (child_x, child_y) not in levels[zoom + 1]:
                        continue
                    built = build(zoom + 1, child_x, child_y)
                    if built is None:
                        continue
                    fog_children[(offset_x, offset_y)] = built[0]
                    trail_children[(offset_x, offset_y)] = built[1]

            if not fog_children:
                return None
            fog = parent_tile(fog_children, "max")
            trail = parent_tile(trail_children, "sum")

        wanted = in_scope(zoom, x, y)
        for theme in themes:
            for kind in kinds:
                destination = tile_path(root, theme, view, kind, zoom, x, y)
                kept.add(destination)
                if not wanted:
                    continue
                rgba = (
                    render_fog(fog, theme, colour=colours[theme])
                    if kind == "fog"
                    else render_trail(trail, theme, ramp, grow_px=trail_grow(zoom))
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(encode_png(rgba))
                written += 1

        # Deliberately the untouched arrays: the level above folds these, and
        # thickening compounds if it is baked in on the way up.
        return fog, trail

    build(0, 0, 0)

    deep_parents = sorted(
        native if scope is None else native & scope.get(geo.NATIVE_Z, set())
    )
    return written, kept, deep_parents


def prune_view(
    root: Path,
    view: str,
    kept: set[Path],
    themes: tuple[str, ...],
    scope: dict[int, set[tuple[int, int]]] | None,
    kinds: tuple[str, ...] = KINDS,
) -> None:
    """Delete rendered tiles this render did not account for.

    Only among the kinds that were rendered. A trail-only pass knows nothing
    about which fog tiles hold data, so sweeping those would delete every one
    of them on the grounds that this pass did not write them.
    """
    if kinds != KINDS:
        return

    if scope is None:
        prune_stale(root, view, kept)
        return

    # Only a tile inside the scope can have lost its data, so the sweep over
    # the whole view is not just wasted - with a view per year it is most of
    # what an edit costs.
    for zoom, tiles in scope.items():
        for tile_x, tile_y in tiles:
            for theme in themes:
                for kind in kinds:
                    path = tile_path(root, theme, view, kind, zoom, tile_x, tile_y)
                    if path not in kept:
                        path.unlink(missing_ok=True)


def count_jobs(
    conn: sqlite3.Connection,
    views: list[str],
    scope: dict[int, set[tuple[int, int]]] | None = None,
) -> int:
    """How many units of work a render would be, without doing any of it.

    The same arithmetic render_views_iter does when it builds its queue: one
    shallow job per view, plus one deep job per native tile that view actually
    has inside the scope. Split out so a caller can say how long the wait will
    be before starting it rather than after.
    """
    jobs = 0
    for view in views:
        native = tiles_with_data(conn, view_layers(view))
        if not native:
            continue
        jobs += 1
        jobs += len(native if scope is None else native & scope.get(geo.NATIVE_Z, set()))
    return jobs


#: The shallow job's stand-in coordinates. A view has one of them and it
#: covers z0 to z13, so it belongs to no single native tile.
SHALLOW = (-1, -1)

JobKey = tuple[str, int, int]


def render_views_iter(
    conn: sqlite3.Connection,
    root: Path,
    views: list[str],
    themes: tuple[str, ...] = THEMES,
    scope: dict[int, set[tuple[int, int]]] | None = None,
    workers: int | None = None,
    written: dict[str, int] | None = None,
    kinds: tuple[str, ...] = KINDS,
    skip: set[JobKey] | None = None,
    on_done: Callable[[JobKey], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> Iterator[tuple[int, int]]:
    """Render a list of views, yielding (jobs finished, jobs total) as it goes.

    The fan-out is over every (view, native tile) pair at once rather than over
    views, and that distinction is most of the speed. Views are wildly uneven -
    the cumulative view covers every tile in the archive while a year view
    might cover five - so a worker per view spends its time watching one job
    finish. One flat queue keeps every core busy until there is nothing left.

    Progress is yielded rather than returned because the only honest thing to
    show during a minute of rendering is how much of it is done, and that has
    to leave this function while it is still working. `written` is filled in
    with the per-view tile counts, since a generator has nowhere good to put a
    return value.

    `skip`, `on_done` and `stop` are what make a render resumable. A queue that
    was interrupted knows which jobs finished, hands them in as `skip`, records
    each new one through `on_done`, and is asked through `stop` whether to keep
    going - so closing a browser or restarting the server costs the job in
    flight and nothing else.

    One caveat for callers writing a script: with more than one worker this
    starts processes through forkserver, which re-imports the caller's main
    module. A script that does its work at import time will do it again in
    every worker. Put it behind `if __name__ == "__main__":`, or pass
    workers=1. The server and the CLI are both already safe - uvicorn owns the
    main module in one, and the other guards it.
    """
    counts: dict[str, int] = {view: 0 for view in views}
    if written is not None:
        written.clear()
        written.update(counts)
        counts = written

    if not views:
        yield 0, 0
        return

    workers = render_workers() if workers is None else workers

    kept: dict[str, set[Path]] = {view: set() for view in views}
    owners: list[str] = []
    keys: list[JobKey] = []
    jobs: list[Job] = []

    database, root_text = db.path_of(conn), str(root)
    for view in views:
        native = tiles_with_data(conn, view_layers(view))
        if not native:
            # Everything in this view is gone. Its tiles have to go with it.
            prune_stale(root, view, set())
            continue

        finished = skip or set()

        if (view, *SHALLOW) not in finished:
            owners.append(view)
            keys.append((view, *SHALLOW))
            jobs.append(
                ("shallow", database, root_text, view, themes, scope, None, kinds)
            )

        parents = native if scope is None else native & scope.get(geo.NATIVE_Z, set())
        for parent in sorted(parents):
            if (view, parent[0], parent[1]) in finished:
                continue
            owners.append(view)
            keys.append((view, parent[0], parent[1]))
            jobs.append(
                ("deep", database, root_text, view, themes, scope, parent, kinds)
            )

    total = len(jobs)
    yield 0, total
    if not jobs:
        return

    def absorb(view: str, result: tuple[int, list[str]]) -> None:
        count, paths = result
        counts[view] += count
        kept[view].update(Path(path) for path in paths)

    done = 0
    if workers > 1 and total >= PARALLEL_FROM:
        # forkserver, not the default fork. The API process runs request
        # handlers in a thread pool, and forking a multi-threaded process can
        # deadlock the child on a lock that was held by a thread that does not
        # exist any more. forkserver forks from a clean single-threaded parent.
        with ProcessPoolExecutor(
            max_workers=min(workers, total),
            mp_context=multiprocessing.get_context("forkserver"),
        ) as pool:
            futures = {
                pool.submit(_render_job, job): (owner, key)
                for job, owner, key in zip(jobs, owners, keys)
            }
            # as_completed rather than map, so a finished job is reported the
            # moment it lands instead of in the order it was queued.
            for future in as_completed(futures):
                owner, key = futures[future]
                absorb(owner, future.result())
                if on_done is not None:
                    on_done(key)
                done += 1
                yield done, total
                if stop is not None and stop():
                    # Cancel what has not started. Jobs already running are let
                    # finish, because killing one mid-write leaves a half tile.
                    for pending in futures:
                        pending.cancel()
                    return
    else:
        for job, owner, key in zip(jobs, owners, keys):
            absorb(owner, _render_job(job))
            if on_done is not None:
                on_done(key)
            done += 1
            yield done, total
            if stop is not None and stop():
                return

    # Pruning deletes tiles inside the scope that this pass did not write, which
    # is how ground whose data has gone stops being drawn. It is only safe when
    # this pass wrote everything it was responsible for.
    #
    # On a resumed pass it is not. Jobs finished before the interruption are
    # handed in as `skip`, so their tiles are absent from `kept` - and pruning
    # would delete them, throwing away exactly the work the resume existed to
    # preserve. That is not a theory: a resume here deleted about 1,300 deep
    # tiles from the cumulative view, which had been rendered first and so was
    # almost entirely skipped.
    #
    # So a resumed pass leaves stale tiles alone. They are removed by the next
    # pass that runs start to finish, which is the cheaper mistake by a wide
    # margin: a lingering tile shows ground you no longer have data for, and a
    # deleted one shows nothing where you do.
    if skip:
        return

    for view in views:
        if kept[view]:
            prune_view(root, view, kept[view], themes, scope, kinds)


def render_views(
    conn: sqlite3.Connection,
    root: Path,
    views: list[str],
    themes: tuple[str, ...] = THEMES,
    scope: dict[int, set[tuple[int, int]]] | None = None,
    workers: int | None = None,
    kinds: tuple[str, ...] = KINDS,
) -> dict[str, int]:
    """Render a list of views. Returns tiles written per view."""
    written: dict[str, int] = {}
    for _ in render_views_iter(
        conn, root, views, themes, scope, workers, written, kinds
    ):
        pass
    return written


def render_view(
    conn: sqlite3.Connection,
    root: Path,
    view: str,
    themes: tuple[str, ...] = THEMES,
    scope: dict[int, set[tuple[int, int]]] | None = None,
    workers: int = 1,
) -> int:
    """Render one view's PNG pyramid, both themes. Returns tiles written."""
    return render_views(conn, root, [view], themes, scope, workers)[view]


def render_workers() -> int:
    """How many views to render at once.

    Defaults to leaving a core free, so a bulk import does not make the rest
    of the machine unusable while it runs.
    """
    raw = settings_env.get("RENDER_WORKERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            raise ValueError(
                f"IRFARAN_RENDER_WORKERS must be a whole number, got {raw!r}. "
                "Unset it to use one less than the number of cores."
            ) from None
    return max(1, (os.cpu_count() or 2) - 1)


def render_all(
    conn: sqlite3.Connection,
    root: Path,
    themes: tuple[str, ...] = THEMES,
    scope: dict[int, set[tuple[int, int]]] | None = None,
) -> dict[str, int]:
    """Render every canonical view. Returns tiles written per view."""
    root.mkdir(parents=True, exist_ok=True)
    write_placeholders(root, conn)

    views = available_views(conn)
    rendered = render_views(conn, root, views, themes, scope)

    # A view can disappear entirely - delete the last event of a year and that
    # year is no longer a view at all. Its directory has to go too.
    wanted = {view.replace(":", "-") for view in views}
    for theme in THEMES:
        theme_root = root / theme
        if not theme_root.is_dir():
            continue
        for directory in theme_root.iterdir():
            if directory.is_dir() and directory.name not in wanted:
                shutil.rmtree(directory, ignore_errors=True)

    return rendered


def rebuild_scope(touched: set[tuple[int, int]]) -> dict[int, set[tuple[int, int]]]:
    """Every tile that needs rebuilding for a set of touched z14 tiles.

    Section 6: rebuild those tiles and all 14 ancestors, and nothing else -
    plus, since the pyramid now goes below z14, their descendants down to
    MAX_Z, which are stamped from the same geometry.
    Returned as {zoom: {(x, y), ...}} with z14 included.
    """
    scope: dict[int, set[tuple[int, int]]] = {geo.NATIVE_Z: set(touched)}
    for tile_x, tile_y in touched:
        for zoom, ancestor_x, ancestor_y in geo.ancestors(tile_x, tile_y):
            scope.setdefault(zoom, set()).add((ancestor_x, ancestor_y))
        for zoom in range(geo.NATIVE_Z + 1, geo.MAX_Z + 1):
            scope.setdefault(zoom, set()).update(
                geo.descendants(tile_x, tile_y, zoom)
            )
    return scope
