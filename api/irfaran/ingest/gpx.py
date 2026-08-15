# SPDX-License-Identifier: AGPL-3.0-or-later
"""GPX parsing.

Tracks only. Waypoints and routes are someone's planned intent rather than a
record of where they went, so they are ignored.
"""

from __future__ import annotations

import gpxpy
import gpxpy.gpx

from irfaran.ingest.common import Fix, Track


def parse(data: bytes | str, filename: str = "upload.gpx") -> list[Track]:
    """Parse GPX bytes into tracks, naming the file in any error raised."""
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data

    try:
        parsed = gpxpy.parse(text)
    except Exception as exc:  # gpxpy raises several unrelated exception types
        raise ValueError(f"{filename} is not readable as GPX ({exc}).") from exc

    tracks: list[Track] = []

    for track_index, gpx_track in enumerate(parsed.tracks):
        name = gpx_track.name or f"track {track_index + 1}"
        fixes: list[Fix] = []
        number = 0

        for segment in gpx_track.segments:
            for point in segment.points:
                number += 1

                if point.latitude is None:
                    raise ValueError(
                        f"GPX point {number} missing latitude in track {name!r} "
                        f"of {filename}."
                    )
                if point.longitude is None:
                    raise ValueError(
                        f"GPX point {number} missing longitude in track {name!r} "
                        f"of {filename}."
                    )

                fixes.append(
                    Fix(
                        lon=float(point.longitude),
                        lat=float(point.latitude),
                        time=point.time,
                    )
                )

        if fixes:
            tracks.append(Track(name=name, fixes=fixes, activity=gpx_track.type))

    if not tracks:
        raise ValueError(
            f"{filename} parsed as GPX but contains no track points. Routes "
            "and waypoints are ignored - Irfaran imports recorded tracks only."
        )

    return tracks
