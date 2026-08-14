# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line entry points.

Output is plain stdout with no colons, so it stays greppable and diffable.
`selfcheck` is the fastest post-deploy signal there is - it prints the running
version, proves the coordinate math against known fixtures, and reports what
is actually in the data directory.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fogmap import __version__, db, geo

# St Stephen's Cathedral, Vienna. A public landmark, used because the ground
# resolution there is quoted in the build plan.
VIENNA_LON = 16.373819
VIENNA_LAT = 48.208488


class Fixture:
    """One expected-versus-actual coordinate-math check."""

    def __init__(self, name: str, expected: str, actual: str, ok: bool) -> None:
        self.name = name
        self.expected = expected
        self.actual = actual
        self.ok = ok

    @classmethod
    def number(
        cls, name: str, expected: float, actual: float, tol: float, digits: int = 6
    ) -> "Fixture":
        return cls(
            name,
            f"{expected:.{digits}f}",
            f"{actual:.{digits}f}",
            abs(expected - actual) <= tol,
        )

    @classmethod
    def exact(cls, name: str, expected: object, actual: object) -> "Fixture":
        return cls(name, str(expected), str(actual), expected == actual)


def geo_fixtures() -> list[Fixture]:
    """The checks from section 4 of the build plan, evaluated live."""
    origin_x, origin_y = geo.lonlat_to_px(0.0, 0.0)
    vienna_tile = geo.lonlat_to_tile(VIENNA_LON, VIENNA_LAT)
    trip_lon, trip_lat = geo.px_to_lonlat(*geo.lonlat_to_px(VIENNA_LON, VIENNA_LAT))
    crossing = geo.split_antimeridian(
        [(179.8, 0.0), (179.9, 0.0), (-179.9, 0.0), (-179.8, 0.0)]
    )

    return [
        Fixture.exact("world px", 4_194_304, geo.WORLD_PX),
        Fixture.exact("tile px", 256, geo.TILE_PX),
        Fixture.exact("native zoom", 14, geo.NATIVE_Z),
        Fixture.number("m per px at equator", 9.5546, geo.m_per_px(0.0), 1e-4, 4),
        Fixture.number("m per px at lat 48.2", 6.37, geo.m_per_px(48.2), 5e-3, 4),
        Fixture.number("null island x px", geo.WORLD_PX / 2, origin_x, 1e-6, 1),
        Fixture.number("null island y px", geo.WORLD_PX / 2, origin_y, 1e-6, 1),
        Fixture.exact("vienna z14 tile x", 8937, vienna_tile[0]),
        Fixture.exact("vienna z14 tile y", 5681, vienna_tile[1]),
        Fixture.number("round trip lon", VIENNA_LON, trip_lon, 1e-6),
        Fixture.number("round trip lat", VIENNA_LAT, trip_lat, 1e-6),
        Fixture.number("latitude clamp north", 85.0511, geo.clamp_lat(90.0), 1e-4, 4),
        Fixture.number("latitude clamp south", -85.0511, geo.clamp_lat(-90.0), 1e-4, 4),
        Fixture.exact("antimeridian segments", 2, len(crossing)),
        Fixture.number(
            "brush 15 m in px at vienna",
            15.0 / geo.m_per_px(VIENNA_LAT),
            geo.radius_px(15.0, VIENNA_LAT),
            1e-9,
        ),
    ]


def count_tiles(root: Path) -> int:
    """Rendered PNG tiles on disk. Zero until the tile pyramid exists."""
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.png"))


def selfcheck() -> int:
    out = sys.stdout.write
    failures = 0

    out("FogMap selfcheck\n\n")
    out(f"version {__version__}\n\n")

    out("coordinate fixtures\n")
    rows = geo_fixtures()
    width = max(len(row.name) for row in rows)
    for row in rows:
        status = "ok" if row.ok else "FAIL"
        if not row.ok:
            failures += 1
        out(
            f"  {row.name.ljust(width)}  expected {row.expected.rjust(12)}"
            f"  actual {row.actual.rjust(12)}  {status}\n"
        )
    out("\n")

    data_root = db.data_dir()
    out("data\n")
    out(f"  directory      {data_root}\n")
    out(f"  database       {db.db_path()}\n")

    try:
        conn = db.open_initialised()
    except RuntimeError as exc:
        out(f"  status         unreadable\n\n{exc}\n")
        return 1

    try:
        table_counts = db.counts(conn)
        by_kind = db.blob_counts_by_kind(conn)
        layers = db.layer_inventory(conn)
    finally:
        conn.close()

    out(f"  events         {table_counts['events']}\n")
    out(f"  places         {table_counts['places']}\n\n")

    out("blobs\n")
    out(f"  total          {table_counts['blobs']}\n")
    for kind in ("fog", "trail", "erase"):
        out(f"  {kind.ljust(14)} {by_kind.get(kind, 0)}\n")
    out("\n")

    out("tiles\n")
    out(f"  rendered png   {count_tiles(data_root / 'tiles')}\n\n")

    out("layers\n")
    if not layers:
        out("  none\n")
    for layer in layers:
        sources = ", ".join(sorted(set(layer["sources"])))  # type: ignore[arg-type]
        out(f"  {str(layer['layer']).ljust(14)} {layer['blobs']} blobs from {sources}\n")
    out("\n")

    if failures:
        out(f"selfcheck FAILED with {failures} bad fixtures\n")
        return 1

    out("selfcheck passed\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m fogmap.cli",
        description="FogMap maintenance commands",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("selfcheck", help="report version, geo fixtures and data inventory")

    args = parser.parse_args(argv)
    if args.command == "selfcheck":
        return selfcheck()

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
