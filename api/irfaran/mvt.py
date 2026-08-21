# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading points out of a vector tile.

A Mapbox Vector Tile is protobuf: layers, each with a name, a list of features,
and side tables of attribute keys and values. A gazetteer wants one narrow thing
from it - the named points in two layers - so that is all this reads. Lines and
polygons are skipped without being decoded.

Written rather than depended on, for the same reason as the Plus Code decoder and
the icons: the wire format is varints and length-delimited fields, point geometry
is a command integer followed by zigzag pairs, and a package for that would be a
package plus its transitive dependencies in an image where most installs will
never switch this feature on.

The schema being read, from the specification:

    Tile.layers      field 3, repeated
    Layer.name       field 1        Layer.features   field 2, repeated
    Layer.keys       field 3        Layer.values     field 4, repeated
    Layer.extent     field 5        Feature.tags     field 2, packed
    Feature.type     field 3        Feature.geometry field 4, packed
"""

from __future__ import annotations

import math
from typing import Iterator

#: Wire types that appear in a vector tile.
VARINT, SIXTY_FOUR, LENGTH, THIRTY_TWO = 0, 1, 2, 5

#: Feature geometry types. Only the first is read.
POINT = 1

DEFAULT_EXTENT = 4096


def _varint(buffer: bytes, position: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def _fields(buffer: bytes, start: int = 0, end: int | None = None) -> Iterator[tuple[int, int, object]]:
    """Every (field number, wire type, value) in a protobuf message.

    A length-delimited value comes back as a (start, end) pair rather than a copy,
    because a planet scan does this a hundred million times and slicing every
    nested message would be most of the work.
    """
    position = start
    limit = len(buffer) if end is None else end
    while position < limit:
        key, position = _varint(buffer, position)
        field, wire = key >> 3, key & 0x07

        if wire == VARINT:
            value, position = _varint(buffer, position)
            yield field, wire, value
        elif wire == LENGTH:
            size, position = _varint(buffer, position)
            yield field, wire, (position, position + size)
            position += size
        elif wire == SIXTY_FOUR:
            yield field, wire, (position, position + 8)
            position += 8
        elif wire == THIRTY_TWO:
            yield field, wire, (position, position + 4)
            position += 4
        else:  # pragma: no cover - not emitted by any tile writer
            raise ValueError(f"Unknown wire type {wire} in a vector tile.")


def _packed(buffer: bytes, span: tuple[int, int]) -> list[int]:
    start, end = span
    out: list[int] = []
    position = start
    while position < end:
        value, position = _varint(buffer, position)
        out.append(value)
    return out


def _string(buffer: bytes, span: tuple[int, int]) -> str:
    return buffer[span[0] : span[1]].decode("utf-8", "replace")


def _value(buffer: bytes, span: tuple[int, int]) -> object:
    """One attribute value, whichever of the seven types it turned out to be."""
    import struct

    for field, _wire, raw in _fields(buffer, *span):
        if field == 1:
            return _string(buffer, raw)  # type: ignore[arg-type]
        if field == 2:
            return struct.unpack_from("<f", buffer, raw[0])[0]  # type: ignore[index]
        if field == 3:
            return struct.unpack_from("<d", buffer, raw[0])[0]  # type: ignore[index]
        if field in (4, 5):
            return int(raw)  # type: ignore[arg-type]
        if field == 6:
            number = int(raw)  # type: ignore[arg-type]
            return (number >> 1) ^ -(number & 1)
        if field == 7:
            return bool(raw)
    return None


def _first_point(buffer: bytes, span: tuple[int, int]) -> tuple[int, int] | None:
    """The first point of a feature's geometry, in tile coordinates.

    A place or a POI is one point. Anything with more is read as its first, which
    is what a label is anchored to anyway.
    """
    numbers = _packed(buffer, span)
    if len(numbers) < 3:
        return None

    command = numbers[0]
    if command & 0x07 != 1:  # not MoveTo
        return None

    x = (numbers[1] >> 1) ^ -(numbers[1] & 1)
    y = (numbers[2] >> 1) ^ -(numbers[2] & 1)
    return x, y


def points(blob: bytes, wanted: set[str]) -> Iterator[tuple[str, dict[str, object], int, int, int]]:
    """Every named point in the wanted layers.

    Yields (layer, attributes, x, y, extent). Coordinates are tile-local; the
    caller knows which tile it asked for and `lonlat` turns them into a position.
    """
    for field, _wire, span in _fields(blob):
        if field != 3:
            continue

        start, end = span  # type: ignore[misc]
        name = ""
        extent = DEFAULT_EXTENT
        keys: list[str] = []
        values: list[object] = []
        features: list[tuple[int, int]] = []

        for inner, _w, raw in _fields(blob, start, end):
            if inner == 1:
                name = _string(blob, raw)  # type: ignore[arg-type]
            elif inner == 2:
                features.append(raw)  # type: ignore[arg-type]
            elif inner == 3:
                keys.append(_string(blob, raw))  # type: ignore[arg-type]
            elif inner == 4:
                values.append(_value(blob, raw))  # type: ignore[arg-type]
            elif inner == 5:
                extent = int(raw)  # type: ignore[arg-type]

        if name not in wanted:
            continue

        for feature in features:
            kind = 0
            tags: list[int] = []
            geometry: tuple[int, int] | None = None

            for inner, _w, raw in _fields(blob, *feature):
                if inner == 2:
                    tags = _packed(blob, raw)  # type: ignore[arg-type]
                elif inner == 3:
                    kind = int(raw)  # type: ignore[arg-type]
                elif inner == 4:
                    geometry = raw  # type: ignore[assignment]

            if kind != POINT or geometry is None:
                continue

            spot = _first_point(blob, geometry)
            if spot is None:
                continue

            attributes: dict[str, object] = {}
            for index in range(0, len(tags) - 1, 2):
                key_index, value_index = tags[index], tags[index + 1]
                if key_index < len(keys) and value_index < len(values):
                    attributes[keys[key_index]] = values[value_index]

            yield name, attributes, spot[0], spot[1], extent


def lonlat(zoom: int, tile_x: int, tile_y: int, x: int, y: int, extent: int) -> tuple[float, float]:
    """Where a tile-local point is on the globe."""
    scale = float(1 << zoom)
    world_x = (tile_x + x / extent) / scale
    world_y = (tile_y + y / extent) / scale

    lon = world_x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * world_y))))
    return lon, lat
