# SPDX-License-Identifier: AGPL-3.0-or-later
"""Command line entry points.

Output is plain stdout with no colons, so it stays greppable and diffable.
`selfcheck` is the fastest post-deploy signal there is - it prints the running
version, proves the coordinate math against known fixtures, and reports what
is actually in the data directory.
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np

from irfaran import __version__, composite, db, geo, raster

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


def blob_digest(conn: sqlite3.Connection) -> str:
    """A stable hash over every blob, in key order.

    This is the number to compare after a re-import or a rebuild. Invariant 1
    says both must leave it unchanged.
    """
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT kind, source, layer, x, y, data FROM blobs "
        "ORDER BY kind, source, layer, x, y"
    ):
        digest.update(
            f"{row['kind']}/{row['source']}/{row['layer']}/{row['x']}/{row['y']}".encode()
        )
        digest.update(row["data"])
    return digest.hexdigest()


def count_tiles(root: Path) -> int:
    """Rendered PNG tiles on disk. Zero until the tile pyramid exists."""
    if not root.is_dir():
        return 0
    return sum(1 for _ in root.rglob("*.png"))


def selfcheck() -> int:
    out = sys.stdout.write
    failures = 0

    out("Irfaran selfcheck\n\n")
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
        digest = blob_digest(conn)
        views = composite.available_views(conn)
    finally:
        conn.close()

    out(f"  events         {table_counts['events']}\n")
    out(f"  places         {table_counts['places']}\n\n")

    out("blobs\n")
    out(f"  total          {table_counts['blobs']}\n")
    for kind in ("fog", "trail", "erase"):
        out(f"  {kind.ljust(14)} {by_kind.get(kind, 0)}\n")
    out(f"  digest         {digest}\n\n")

    out("tiles\n")
    out(f"  rendered png   {count_tiles(data_root / 'tiles')}\n\n")

    out("layers\n")
    if not layers:
        out("  none\n")
    for layer in layers:
        sources = ", ".join(sorted(set(layer["sources"])))  # type: ignore[arg-type]
        out(f"  {str(layer['layer']).ljust(14)} {layer['blobs']} blobs from {sources}\n")
    out("\n")

    out("views\n")
    for view in views:
        out(f"  {view}\n")
    out("\n")

    if failures:
        out(f"selfcheck FAILED with {failures} bad fixtures\n")
        return 1

    out("selfcheck passed\n")
    return 0


def rebuild() -> int:
    """Wipe every blob and replay the event log."""
    out = sys.stdout.write
    conn = db.open_initialised()
    try:
        before = blob_digest(conn)
        events = db.counts(conn)["events"]
        out(f"replaying {events} events\n")

        replayed, touched = raster.rebuild(conn)
        after = blob_digest(conn)
        blobs = db.counts(conn)["blobs"]
    finally:
        conn.close()

    out(f"  events replayed  {replayed}\n")
    out(f"  z14 tiles        {len(touched)}\n")
    out(f"  blobs written    {blobs}\n")
    out(f"  digest before    {before}\n")
    out(f"  digest after     {after}\n")
    out(
        "rebuild reproduced the previous blobs exactly\n"
        if before == after
        else "rebuild CHANGED the blobs\n"
    )
    return 0


def render(args: argparse.Namespace) -> int:
    """Render the PNG tile pyramid for every canonical view, both themes."""
    out = sys.stdout.write
    root = db.data_dir() / "tiles"

    conn = db.open_initialised()
    try:
        views = (
            [args.view] if args.view else composite.available_views(conn)
        )
        root.mkdir(parents=True, exist_ok=True)
        composite.write_placeholders(root, conn)

        out(f"rendering {len(views)} views into {root}\n")
        total = 0
        for view in views:
            started = time.monotonic()
            written = composite.render_view(conn, root, view)
            total += written
            out(
                f"  {view.ljust(16)} {written} tiles in "
                f"{time.monotonic() - started:.1f}s\n"
            )
    finally:
        conn.close()

    out(f"wrote {total} tiles\n")
    return 0


def dump_blob(args: argparse.Namespace) -> int:
    """Write one z14 tile to a PNG for visual inspection.

    Deliberately plain greyscale, not the themed colourmap - this is for
    eyeballing whether a track looks coherent, not for the map.
    """
    from PIL import Image

    out = sys.stdout.write
    conn = db.open_initialised()
    try:
        if args.source and args.layer:
            array = raster.read_blob(
                conn, args.kind, args.source, args.layer, args.x, args.y
            )
            if array is None:
                out(
                    f"no {args.kind} blob at x {args.x} y {args.y} for source "
                    f"{args.source} layer {args.layer}\n"
                )
                return 1
            source_note = f"{args.source} {args.layer}"
        else:
            fog, trail = composite.composite_tile(conn, args.view, args.x, args.y)
            if args.kind == "trail":
                array = trail
            elif args.kind == "fog":
                array = np.where(fog, 255, 0).astype(np.uint8)
            else:
                array = np.where(
                    composite.erase_mask(conn, args.x, args.y), 255, 0
                ).astype(np.uint8)
            source_note = f"view {args.view}"
    finally:
        conn.close()

    painted = int((array > 0).sum())
    if args.kind == "trail" and array.max() > 0:
        # Stretch the pass count so a one-pass track is visible rather than
        # a single grey level away from black.
        array = (array.astype(np.float64) / float(array.max()) * 255).astype(np.uint8)

    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(array, mode="L").save(destination)

    out(f"wrote {destination}\n")
    out(f"  tile           z14 x {args.x} y {args.y}\n")
    out(f"  kind           {args.kind}\n")
    out(f"  from           {source_note}\n")
    out(f"  painted px     {painted} of {geo.TILE_PX * geo.TILE_PX}\n")
    return 0


def import_file(args: argparse.Namespace) -> int:
    """Import a GPX or TCX file straight from disk."""
    from irfaran.ingest import common, gpx, tcx

    out = sys.stdout.write
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"No such file {path}")

    payload = path.read_bytes()
    parser = tcx if path.suffix.lower() == ".tcx" else gpx
    tracks = parser.parse(payload, filename=path.name)

    conn = db.open_initialised()
    try:
        result = common.ingest_tracks(conn, args.source, tracks)
        digest = blob_digest(conn)
    finally:
        conn.close()

    out(f"imported {path.name}\n")
    out(f"  tracks parsed    {len(tracks)}\n")
    out(f"  events created   {result.events_created}\n")
    out(f"  events skipped   {result.events_skipped}\n")
    out(f"  points stamped   {result.points}\n")
    out(f"  points dropped   {result.points_dropped}\n")
    out(f"  z14 tiles        {len(result.tiles_touched)}\n")
    out(f"  digest           {digest}\n")
    return 0


def show_token() -> int:
    """Print the token this server is using.

    The way back in. Once setup is finished the token is never served over
    HTTP again, so the console is the only place left to read it - and the
    console is the one place where being able to read it means you own the
    machine anyway.
    """
    from irfaran import tokens

    out = sys.stdout.write
    conn = db.open_initialised()
    try:
        value, source = tokens.resolve(conn)
    finally:
        conn.close()

    out(f"{value}\n")
    out(
        f"from the environment ({tokens.ENV_NAME})\n"
        if source == "environment"
        else "generated on first start and stored in the database\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m irfaran.cli",
        description="Irfaran maintenance commands",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("selfcheck", help="report version, geo fixtures and data inventory")
    sub.add_parser("token", help="print the API token this server is using")
    sub.add_parser("rebuild", help="wipe every blob and replay the event log")

    draw = sub.add_parser("render", help="render the PNG tile pyramid")
    draw.add_argument("--view", help="one view only, default every view")

    dump = sub.add_parser("dump-blob", help="write one z14 tile to a PNG")
    dump.add_argument("--x", type=int, required=True, help="z14 tile x")
    dump.add_argument("--y", type=int, required=True, help="z14 tile y")
    dump.add_argument(
        "--kind", choices=["fog", "trail", "erase"], required=True
    )
    dump.add_argument("--out", required=True, help="destination PNG path")
    dump.add_argument("--source", help="one source only, requires --layer")
    dump.add_argument("--layer", help="one layer only, requires --source")
    dump.add_argument("--view", default="all", help="composited view, default all")

    load = sub.add_parser("import", help="import a GPX or TCX file from disk")
    load.add_argument("--file", required=True)
    load.add_argument("--source", default="workout")

    args = parser.parse_args(argv)

    if args.command == "selfcheck":
        return selfcheck()
    if args.command == "token":
        return show_token()
    if args.command == "rebuild":
        return rebuild()
    if args.command == "render":
        return render(args)
    if args.command == "dump-blob":
        return dump_blob(args)
    if args.command == "import":
        return import_file(args)

    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
