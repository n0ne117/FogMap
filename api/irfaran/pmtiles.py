# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading the basemap archive directly.

PMTiles is a single file holding every tile of a map, addressed by a Hilbert
curve so that neighbouring tiles sit near each other on disk. The browser reads
it over HTTP range requests; this reads it locally, which is what a gazetteer
build needs - it has to walk the whole thing rather than fetch a tile at a time.

Written here rather than taken as a dependency. The format is a fixed 127-byte
header, directories of varints, and a blob of tile data, and what is needed from
it is small: walk every entry, fetch a tile, know which z/x/y it was. The same
reasoning as the hand-written Plus Code decoder - a package for this would be a
package for three functions.

Nothing here writes. The archive is 137 GB of somebody else's build and this
opens it read-only.
"""

from __future__ import annotations

import gzip
import json
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

MAGIC = b"PMTiles"
HEADER_LENGTH = 127

#: Compression values from the specification.
NONE, GZIP, BROTLI, ZSTD = 1, 2, 3, 4


class ArchiveError(ValueError):
    """The file is not a PMTiles archive, or not one this can read."""


@dataclass(frozen=True)
class Header:
    root_offset: int
    root_length: int
    metadata_offset: int
    metadata_length: int
    leaf_offset: int
    leaf_length: int
    data_offset: int
    data_length: int
    addressed_tiles: int
    tile_entries: int
    tile_contents: int
    clustered: bool
    internal_compression: int
    tile_compression: int
    tile_type: int
    min_zoom: int
    max_zoom: int


@dataclass(frozen=True)
class Entry:
    """One directory entry: a tile id, where its bytes are, and how many ids share them."""

    tile_id: int
    offset: int
    length: int
    run_length: int


def _varint(buffer: bytes, position: int) -> tuple[int, int]:
    """One base-128 varint, and where it ended."""
    value = 0
    shift = 0
    while True:
        byte = buffer[position]
        position += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, position
        shift += 7


def _decompress(raw: bytes, kind: int) -> bytes:
    if kind in (NONE, 0):
        return raw
    if kind == GZIP:
        return gzip.decompress(raw)
    if kind == ZSTD:  # pragma: no cover - not produced by the planet builds
        raise ArchiveError("This archive is zstd compressed, which is not read here.")
    if kind == BROTLI:  # pragma: no cover
        raise ArchiveError("This archive is brotli compressed, which is not read here.")
    # Some writers mark deflate as gzip and vice versa; try the other one rather
    # than failing on a file that is perfectly readable.
    return zlib.decompress(raw)


def tile_id_to_zxy(tile_id: int) -> tuple[int, int, int]:
    """Where on the map a Hilbert tile id lands.

    The ids run zoom by zoom - one tile at z0, four at z1 - and within a zoom
    they follow a Hilbert curve, which is what keeps neighbours close together in
    the file.
    """
    acc = 0
    zoom = 0
    while True:
        count = 1 << (zoom * 2)
        if acc + count > tile_id:
            return (zoom, *_hilbert_to_xy(zoom, tile_id - acc))
        acc += count
        zoom += 1


def _hilbert_to_xy(zoom: int, position: int) -> tuple[int, int]:
    x = y = 0
    side = 1
    remaining = position
    while side < (1 << zoom):
        rx = 1 & (remaining // 2)
        ry = 1 & (remaining ^ rx)
        x, y = _rotate(side, x, y, rx, ry)
        x += side * rx
        y += side * ry
        remaining //= 4
        side *= 2
    return x, y


def _rotate(side: int, x: int, y: int, rx: int, ry: int) -> tuple[int, int]:
    if ry == 0:
        if rx == 1:
            x = side - 1 - x
            y = side - 1 - y
        return y, x
    return x, y


def zxy_to_tile_id(zoom: int, x: int, y: int) -> int:
    """The Hilbert id of a tile, for fetching one directly."""
    acc = sum(1 << (level * 2) for level in range(zoom))
    return acc + _xy_to_hilbert(zoom, x, y)


def _xy_to_hilbert(zoom: int, x: int, y: int) -> int:
    position = 0
    side = 1 << (zoom - 1) if zoom else 0
    while side > 0:
        rx = 1 if x & side else 0
        ry = 1 if y & side else 0
        position += side * side * ((3 * rx) ^ ry)
        x, y = _rotate(side, x, y, rx, ry)
        side //= 2
    return position


class Archive:
    """A PMTiles file, open for reading."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._file = self.path.open("rb")
        self.header = self._read_header()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Archive:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _read_header(self) -> Header:
        self._file.seek(0)
        raw = self._file.read(HEADER_LENGTH)
        if raw[: len(MAGIC)] != MAGIC:
            raise ArchiveError(f"{self.path} does not begin with {MAGIC!r}.")

        numbers = struct.unpack_from("<11Q", raw, 8)
        flags = struct.unpack_from("<6B", raw, 96)
        return Header(
            *numbers,
            clustered=bool(flags[0]),
            internal_compression=flags[1],
            tile_compression=flags[2],
            tile_type=flags[3],
            min_zoom=flags[4],
            max_zoom=flags[5],
        )

    def metadata(self) -> dict[str, object]:
        """The archive's own description of itself, including its layers."""
        self._file.seek(self.header.metadata_offset)
        raw = self._file.read(self.header.metadata_length)
        text = _decompress(raw, self.header.internal_compression)
        loaded = json.loads(text)
        return loaded if isinstance(loaded, dict) else {}

    def _directory(self, offset: int, length: int) -> list[Entry]:
        self._file.seek(offset)
        buffer = _decompress(self._file.read(length), self.header.internal_compression)

        count, position = _varint(buffer, 0)

        ids: list[int] = []
        last = 0
        for _ in range(count):
            delta, position = _varint(buffer, position)
            last += delta
            ids.append(last)

        runs: list[int] = []
        for _ in range(count):
            value, position = _varint(buffer, position)
            runs.append(value)

        lengths: list[int] = []
        for _ in range(count):
            value, position = _varint(buffer, position)
            lengths.append(value)

        offsets: list[int] = []
        for index in range(count):
            value, position = _varint(buffer, position)
            # Zero means "immediately after the previous one", which is how a
            # clustered archive avoids storing an offset per tile.
            if value == 0 and index > 0:
                offsets.append(offsets[index - 1] + lengths[index - 1])
            else:
                offsets.append(value - 1)

        return [
            Entry(ids[index], offsets[index], lengths[index], runs[index])
            for index in range(count)
        ]

    def zoom_range(self, zoom: int) -> tuple[int, int]:
        """The first and last Hilbert id belonging to a zoom.

        Ids run zoom by zoom, so a whole level is one contiguous range - which is
        what makes scanning a single zoom cheap instead of a walk over all 177
        million entries.
        """
        first = sum(1 << (level * 2) for level in range(zoom))
        return first, first + (1 << (zoom * 2)) - 1

    def entries_between(self, first_id: int, last_id: int) -> Iterator[Entry]:
        """Every entry in a range of tile ids, skipping leaves that cannot hold any.

        The root directory is sorted, so a leaf covers from its own id up to the
        next leaf's - and a leaf whose span misses the range is never read.
        """
        root = self._directory(self.header.root_offset, self.header.root_length)

        for index, entry in enumerate(root):
            if entry.run_length != 0:
                if first_id <= entry.tile_id <= last_id:
                    yield entry
                continue

            starts = entry.tile_id
            ends = root[index + 1].tile_id - 1 if index + 1 < len(root) else last_id
            if ends < first_id or starts > last_id:
                continue

            for leaf in self._directory(
                self.header.leaf_offset + entry.offset, entry.length
            ):
                if leaf.run_length == 0:
                    continue
                if first_id <= leaf.tile_id <= last_id:
                    yield leaf

    def entries(self) -> Iterator[Entry]:
        """Every tile entry in the archive, leaf directories walked as they come.

        Yields the entries that point at tile data; a run_length of zero means a
        leaf directory, which is followed rather than returned.
        """
        yield from self._walk(self.header.root_offset, self.header.root_length)

    def _walk(self, offset: int, length: int) -> Iterator[Entry]:
        for entry in self._directory(offset, length):
            if entry.run_length == 0:
                yield from self._walk(
                    self.header.leaf_offset + entry.offset, entry.length
                )
            else:
                yield entry

    def tile(self, zoom: int, x: int, y: int) -> bytes | None:
        """One tile by position, or None if the archive has nothing there."""
        wanted = zxy_to_tile_id(zoom, x, y)
        for entry in self.entries():
            if entry.tile_id <= wanted < entry.tile_id + max(entry.run_length, 1):
                return self.blob(entry)
            if entry.tile_id > wanted:
                return None
        return None

    def blob(self, entry: Entry) -> bytes:
        """The decompressed bytes of one tile."""
        self._file.seek(self.header.data_offset + entry.offset)
        return _decompress(self._file.read(entry.length), self.header.tile_compression)
