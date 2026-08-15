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
import os
import shutil
import sqlite3
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from fogmap import geo, raster

TILE = geo.TILE_PX
HALF = TILE // 2

VIEW_ALL = "all"
VIEW_PREHISTORY = "prehistory"
YEAR_PREFIX = "year:"

THEMES = ("light", "dark")
KINDS = ("fog", "trail")

# How far the fog fades out from explored ground. The build plan calls edge
# softening the difference between "a map" and Fog of World, and warns that
# tuning it is an evening on its own - hence the environment override.
DEFAULT_FOG_EDGE_PX = 2.5

# Fog is drawn where the ground is UNEXPLORED, so this is the colour of the
# unknown.
FOG_COLOUR = {
    "dark": (9, 11, 15),
    "light": (236, 236, 231),
}

# How solid the baked fog is, 0 to 255.
#
# Fully opaque, as section 8 says. How much of the map shows through is a
# viewing choice rather than a property of the data, so it is made in the
# browser with MapLibre's raster-opacity - which costs nothing, applies
# instantly and needs no re-render. Baking it in here would fix one answer
# into every tile and still be wrong for the other theme.
DEFAULT_FOG_ALPHA = 255


def fog_alpha() -> int:
    raw = os.environ.get("FOGMAP_FOG_ALPHA", "").strip()
    if not raw:
        return DEFAULT_FOG_ALPHA
    try:
        value = int(float(raw))
    except ValueError:
        raise ValueError(
            f"FOGMAP_FOG_ALPHA must be a number from 0 to 255, got {raw!r}. "
            f"Unset it to use the default of {DEFAULT_FOG_ALPHA}."
        ) from None
    return max(0, min(255, value))


# Pass count to colour. Counts are heavily skewed - most pixels are crossed
# once - so the ramp is walked on a log scale, otherwise every trail but the
# daily commute renders as the same dim first step.
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


def trail_lut(theme: str) -> np.ndarray:
    """A 256-entry pass-count to RGBA lookup table.

    Counts are mapped through log1p before the ramp is walked, so the
    difference between one pass and four is visible rather than being crushed
    into the bottom of a linear scale.
    """
    try:
        anchors = TRAIL_RAMPS[theme]
    except KeyError:
        raise ValueError(
            f"Unknown theme {theme!r}. FogMap renders {' and '.join(THEMES)}."
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
    raw = os.environ.get("FOGMAP_FOG_EDGE_PX", "").strip()
    if not raw:
        return DEFAULT_FOG_EDGE_PX
    try:
        return max(0.0, float(raw))
    except ValueError:
        raise ValueError(
            f"FOGMAP_FOG_EDGE_PX must be a number, got {raw!r}. Unset it to "
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
) -> np.ndarray:
    """Paint the unexplored ground, leaving explored ground transparent."""
    try:
        colour = FOG_COLOUR[theme]
    except KeyError:
        raise ValueError(
            f"Unknown theme {theme!r}. FogMap renders {' and '.join(THEMES)}."
        ) from None

    radius = fog_edge_px() if edge_px is None else edge_px
    solid = fog_alpha() if alpha is None else alpha
    opacity = soften(fog, radius)

    rgba = np.empty((TILE, TILE, 4), dtype=np.uint8)
    rgba[..., 0] = colour[0]
    rgba[..., 1] = colour[1]
    rgba[..., 2] = colour[2]
    rgba[..., 3] = np.clip(opacity * solid, 0, 255).astype(np.uint8)
    return rgba


def render_trail(trail: np.ndarray, theme: str) -> np.ndarray:
    return trail_lut(theme)[trail]


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


def placeholder_tile(theme: str, kind: str) -> bytes:
    """The tile served where nothing has been rendered.

    Fog covers the whole world, so a tile with no data is not missing - it is
    entirely unexplored, and must come back as solid fog. Returning 404 here
    would punch a hole in the fog over every part of the world the user has
    never been, which is most of it.
    """
    if kind == "fog":
        return encode_png(render_fog(np.zeros((TILE, TILE), dtype=bool), theme))
    return encode_png(render_trail(np.zeros((TILE, TILE), dtype=np.uint8), theme))


def write_placeholders(root: Path) -> list[Path]:
    written = []
    for theme in THEMES:
        for kind in KINDS:
            destination = root / theme / f"empty-{kind}.png"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(placeholder_tile(theme, kind))
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


def render_view(
    conn: sqlite3.Connection, root: Path, view: str, themes: tuple[str, ...] = THEMES
) -> int:
    """Render one view's whole PNG pyramid, both themes. Returns tiles written.

    Walks depth first from z0, so at most four tiles per zoom are held at once
    and memory stays flat however large the archive grows.
    """
    native = tiles_with_data(conn, view_layers(view))
    if not native:
        # Everything in this view is gone. Its tiles have to go with it.
        prune_stale(root, view, set())
        return 0

    levels = pyramid_levels(native)
    written = 0
    kept: set[Path] = set()

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

        for theme in themes:
            for kind, rgba in (
                ("fog", render_fog(fog, theme)),
                ("trail", render_trail(trail, theme)),
            ):
                destination = tile_path(root, theme, view, kind, zoom, x, y)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(encode_png(rgba))
                kept.add(destination)
                written += 1

        return fog, trail

    build(0, 0, 0)
    prune_stale(root, view, kept)
    return written


def render_all(
    conn: sqlite3.Connection, root: Path, themes: tuple[str, ...] = THEMES
) -> dict[str, int]:
    """Render every canonical view. Returns tiles written per view."""
    root.mkdir(parents=True, exist_ok=True)
    write_placeholders(root)

    views = available_views(conn)
    rendered = {view: render_view(conn, root, view, themes) for view in views}

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

    Section 6: rebuild those tiles and all 14 ancestors, and nothing else.
    Returned as {zoom: {(x, y), ...}} with z14 included.
    """
    scope: dict[int, set[tuple[int, int]]] = {geo.NATIVE_Z: set(touched)}
    for tile_x, tile_y in touched:
        for zoom, ancestor_x, ancestor_y in geo.ancestors(tile_x, tile_y):
            scope.setdefault(zoom, set()).add((ancestor_x, ancestor_y))
    return scope
