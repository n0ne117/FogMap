# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic test fixtures.

Every coordinate here is generated, not recorded. Tracks sit in the open ocean
in the Gulf of Guinea near Null Island, which is both obviously fake and far
from anywhere anyone lives. No real location data may enter this repository -
a fog map centred on someone's home is a map to their home.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Open water, roughly 300 km south of Ghana. Nobody's front door.
BASE_LON = 0.5
BASE_LAT = 0.25

# About 11 m at the equator, so consecutive points are a sensible stride apart.
STEP_DEG = 0.0001


def straight_line(count: int = 40, base_lon: float = BASE_LON) -> list[tuple[float, float]]:
    """A due-east line of `count` points."""
    return [(base_lon + index * STEP_DEG, BASE_LAT) for index in range(count)]


def square_loop(side: int = 30) -> list[tuple[float, float]]:
    """A closed square, so a dumped tile shows something obviously coherent."""
    points: list[tuple[float, float]] = []
    for index in range(side):
        points.append((BASE_LON + index * STEP_DEG, BASE_LAT))
    for index in range(side):
        points.append((BASE_LON + side * STEP_DEG, BASE_LAT + index * STEP_DEG))
    for index in range(side):
        points.append((BASE_LON + (side - index) * STEP_DEG, BASE_LAT + side * STEP_DEG))
    for index in range(side):
        points.append((BASE_LON, BASE_LAT + (side - index) * STEP_DEG))
    return points


def gpx_document(
    points: list[tuple[float, float]],
    name: str = "Synthetic Loop",
    start: datetime | None = None,
    step_seconds: float = 5.0,
    with_time: bool = True,
) -> str:
    """Build a GPX 1.1 document from a list of lon/lat pairs."""
    clock = start or datetime(2024, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

    rows = []
    for index, (lon, lat) in enumerate(points):
        if with_time:
            stamp = (clock + timedelta(seconds=step_seconds * index)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            time_element = f"<time>{stamp}</time>"
        else:
            time_element = ""
        rows.append(
            f'      <trkpt lat="{lat:.7f}" lon="{lon:.7f}">{time_element}</trkpt>'
        )

    joined = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="FogMap tests" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
        "  <trk>\n"
        f"    <name>{name}</name>\n"
        "    <type>running</type>\n"
        "    <trkseg>\n"
        f"{joined}\n"
        "    </trkseg>\n"
        "  </trk>\n"
        "</gpx>\n"
    )


def tcx_document(
    points: list[tuple[float, float]],
    activity_id: str = "2024-03-01T09:00:00Z",
    sport: str = "Running",
    start: datetime | None = None,
    step_seconds: float = 5.0,
) -> str:
    """Build a Garmin TrainingCenterDatabase document."""
    clock = start or datetime(2024, 3, 1, 9, 0, 0, tzinfo=timezone.utc)

    rows = []
    for index, (lon, lat) in enumerate(points):
        stamp = (clock + timedelta(seconds=step_seconds * index)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        rows.append(
            "          <Trackpoint>\n"
            f"            <Time>{stamp}</Time>\n"
            "            <Position>\n"
            f"              <LatitudeDegrees>{lat:.7f}</LatitudeDegrees>\n"
            f"              <LongitudeDegrees>{lon:.7f}</LongitudeDegrees>\n"
            "            </Position>\n"
            "          </Trackpoint>"
        )

    joined = "\n".join(rows)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/'
        'TrainingCenterDatabase/v2">\n'
        "  <Activities>\n"
        f'    <Activity Sport="{sport}">\n'
        f"      <Id>{activity_id}</Id>\n"
        '      <Lap StartTime="' + activity_id + '">\n'
        "        <Track>\n"
        f"{joined}\n"
        "        </Track>\n"
        "      </Lap>\n"
        "    </Activity>\n"
        "  </Activities>\n"
        "</TrainingCenterDatabase>\n"
    )
