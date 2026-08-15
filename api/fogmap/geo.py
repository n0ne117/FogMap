# SPDX-License-Identifier: AGPL-3.0-or-later
"""Web-Mercator coordinate math on the native FogMap grid.

Pure functions with no I/O and no state. Everything that converts between
geographic coordinates and the raster grid lives here so it can be tested
in isolation.

The native grid is web-Mercator z14 with 256x256 pixel tiles, which is the
same grid Fog of World uses: 512 tiles x 128 blocks x 64 px = 4_194_304 px
= 256 * 2**14.
"""

from __future__ import annotations

import math

WORLD_PX = 256 * 2**14
TILE_PX = 256
NATIVE_Z = 14

# The deepest zoom the PNG pyramid is rendered to.
#
# Everything stored is on the native z14 grid, where a 15 m brush is a
# two-and-a-bit pixel disc. Left there, the client magnifies it sixteen times
# to reach z18 and a hand-drawn stroke arrives as a blurred, visibly stepped
# smear. z15 and z16 are stamped from the same geometry at their own
# resolution instead, which is four times sharper and costs about sixteen
# times the deepest-level tiles.
MAX_Z = 16

EARTH_CIRCUMFERENCE_M = 40_075_016.686
M_PER_PX_EQ = EARTH_CIRCUMFERENCE_M / WORLD_PX

# The latitude at which the Mercator projection reaches y = 0 and y = WORLD_PX.
# Everything beyond this is clamped rather than projected to infinity.
MAX_LAT = 85.05112877980659


def clamp_lat(lat: float) -> float:
    """Clamp a latitude to the Mercator limit of +/- 85.0511 degrees."""
    lat = _finite(lat, "latitude")
    if lat > MAX_LAT:
        return MAX_LAT
    if lat < -MAX_LAT:
        return -MAX_LAT
    return lat


def wrap_lon(lon: float) -> float:
    """Wrap a longitude into [-180, 180]."""
    lon = _finite(lon, "longitude")
    if -180.0 <= lon <= 180.0:
        return lon
    wrapped = math.fmod(lon + 180.0, 360.0)
    if wrapped < 0.0:
        wrapped += 360.0
    return wrapped - 180.0


def world_px(zoom: int = NATIVE_Z) -> int:
    """Width of the world in pixels at a zoom, with 256 px tiles."""
    return TILE_PX * 2**zoom


def lonlat_to_px(
    lon: float, lat: float, zoom: int = NATIVE_Z
) -> tuple[float, float]:
    """Project lon/lat to fractional pixel coordinates on a world grid.

    Returns (x_px, y_px) with the origin at the north-west corner, x growing
    east and y growing south. Latitude is clamped and longitude wrapped, so
    this never raises on in-range-ish input.

    Defaults to the native z14 grid, which is the grid everything stored is
    on. The zoom argument exists for rendering below it: the PNG pyramid goes
    deeper than z14 so that a brush stroke is not a handful of pixels being
    magnified sixteen times, and those levels are stamped from geometry at
    their own resolution rather than upscaled.
    """
    lon = wrap_lon(lon)
    lat_rad = math.radians(clamp_lat(lat))
    span = world_px(zoom)

    x_px = (lon + 180.0) / 360.0 * span
    merc = math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad))
    y_px = (1.0 - merc / math.pi) / 2.0 * span
    return x_px, y_px


def px_to_lonlat(x_px: float, y_px: float) -> tuple[float, float]:
    """Inverse of lonlat_to_px. Exact round trip inside the clamp limits."""
    x_px = _finite(x_px, "x pixel")
    y_px = _finite(y_px, "y pixel")

    lon = x_px / WORLD_PX * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y_px / WORLD_PX))))
    return lon, lat


def m_per_px(lat: float, zoom: int = NATIVE_Z) -> float:
    """Ground resolution in metres per pixel at the given latitude.

    9.5546 m at the equator on the z14 grid, 6.37 m at Vienna's 48.2 degrees.
    """
    at_equator = EARTH_CIRCUMFERENCE_M / world_px(zoom)
    return at_equator * math.cos(math.radians(clamp_lat(lat)))


def radius_px(radius_m: float, lat: float, zoom: int = NATIVE_Z) -> float:
    """Convert a brush radius in metres to pixels at the given latitude.

    Call this per stamped point rather than once per track. Ground resolution
    changes by a factor of two between Vienna and the equator, so a radius
    computed once for a whole track drifts meaningfully.
    """
    radius_m = _finite(radius_m, "brush radius")
    if radius_m <= 0.0:
        raise ValueError(
            f"brush radius must be greater than 0 m, got {radius_m} m"
        )
    return radius_m / m_per_px(lat, zoom)


def px_to_tile(x_px: float, y_px: float) -> tuple[int, int]:
    """The tile containing a pixel coordinate, on that pixel's own grid."""
    return int(math.floor(x_px / TILE_PX)), int(math.floor(y_px / TILE_PX))


def lonlat_to_tile(lon: float, lat: float, zoom: int = NATIVE_Z) -> tuple[int, int]:
    """The tile containing a geographic coordinate."""
    return px_to_tile(*lonlat_to_px(lon, lat, zoom))


def tile_origin_px(tile_x: int, tile_y: int) -> tuple[int, int]:
    """North-west corner of a z14 tile, in world pixels."""
    return tile_x * TILE_PX, tile_y * TILE_PX


def tile_count(zoom: int) -> int:
    """Number of tiles along one axis at the given zoom."""
    if not 0 <= zoom <= MAX_Z:
        raise ValueError(f"zoom must be between 0 and {MAX_Z}, got {zoom}")
    return 2**zoom


def descendants(
    tile_x: int, tile_y: int, zoom: int, of_zoom: int = NATIVE_Z
) -> list[tuple[int, int]]:
    """Every tile at `zoom` inside one tile at `of_zoom`."""
    if zoom < of_zoom:
        raise ValueError(
            f"zoom {zoom} is above {of_zoom}, so there are no descendants there."
        )
    factor = 2 ** (zoom - of_zoom)
    return [
        (tile_x * factor + dx, tile_y * factor + dy)
        for dx in range(factor)
        for dy in range(factor)
    ]


def ancestors(tile_x: int, tile_y: int) -> list[tuple[int, int, int]]:
    """The 14 ancestor tiles of a z14 tile, from z13 down to z0.

    Returned as (zoom, x, y), nearest ancestor first. This is the rebuild
    scope for one touched native tile.
    """
    out: list[tuple[int, int, int]] = []
    x, y = tile_x, tile_y
    for zoom in range(NATIVE_Z - 1, -1, -1):
        x //= 2
        y //= 2
        out.append((zoom, x, y))
    return out


def crosses_antimeridian(lon_a: float, lon_b: float) -> bool:
    """True if the shortest path between two longitudes crosses +/- 180.

    A track that jumps from 179.9 to -179.9 has moved 0.2 degrees east, but
    projected naively it draws a brush stroke straight across the entire
    world. Detecting the crossing is how that is avoided.
    """
    return abs(wrap_lon(lon_b) - wrap_lon(lon_a)) > 180.0


def split_antimeridian(
    points: list[tuple[float, float]],
) -> list[list[tuple[float, float]]]:
    """Split a lon/lat path into segments that never cross the antimeridian.

    Each returned segment can be projected and stamped directly. Segments of
    a single point are kept, since a lone fix is still a valid stamp.
    """
    if not points:
        return []

    segments: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = [points[0]]

    for index in range(1, len(points)):
        prev_lon, _ = points[index - 1]
        lon, _ = points[index]
        if crosses_antimeridian(prev_lon, lon):
            segments.append(current)
            current = [points[index]]
        else:
            current.append(points[index])

    segments.append(current)
    return segments


def _finite(value: float, what: str) -> float:
    """Reject NaN, infinities and non-numeric input with a readable message."""
    try:
        value = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be a number, got {value!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{what} must be a finite number, got {value!r}")
    return value
