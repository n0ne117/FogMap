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

import sqlite3

import numpy as np

from fogmap import geo, raster

TILE = geo.TILE_PX
HALF = TILE // 2

VIEW_ALL = "all"
VIEW_PREHISTORY = "prehistory"
YEAR_PREFIX = "year:"


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
