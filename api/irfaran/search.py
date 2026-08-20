# SPDX-License-Identifier: AGPL-3.0-or-later
"""Search, starting with coordinates.

The basemap cannot be searched. It is 137 GB of rendered vector tiles, so a
place name exists in it as geometry to draw at a zoom rather than as an index,
and answering "where is X" would mean scanning the archive. Searching this map
therefore means searching what Irfaran itself holds - and, before any of that,
understanding a coordinate somebody pasted, which needs no index at all.

Parsing lives on the server rather than in the browser for two reasons. It is
testable here, next to everything else that has to keep working; and the shape
of the answer - a list of results, each with somewhere to go - is the shape the
rest of search will need when pins and tracks join it. Nothing leaves the
machine either way.
"""

from __future__ import annotations

import re
import sqlite3

#: A signed decimal number. Allows a leading + and a bare `.5`.
_NUMBER = r"[-+]?(?:\d+(?:\.\d+)?|\.\d+)"

#: 27.74367, -15.58338 - and the same with a space, or a degree sign, or a
#: hemisphere letter on either side of either number.
_DECIMAL = re.compile(
    rf"""^\s*
    (?P<first_sign>[NSns])?\s*
    (?P<first>{_NUMBER})\s*°?\s*
    (?P<first_suffix>[NSns])?
    \s*(?:,|\s)\s*
    (?P<second_sign>[EWew])?\s*
    (?P<second>{_NUMBER})\s*°?\s*
    (?P<second_suffix>[EWew])?
    \s*$""",
    re.VERBOSE,
)

#: 27°44'37.2"N 15°35'00.2"W, and the sloppier forms of the same thing. The
#: quote marks are whatever a keyboard or a website felt like emitting.
_DMS_PART = r"""
    (?P<{name}_lead>[NSEWnsew])?\s*
    (?P<{name}_deg>\d+(?:\.\d+)?)\s*[°d]\s*
    (?:(?P<{name}_min>\d+(?:\.\d+)?)\s*['′’m]?\s*)?
    (?:(?P<{name}_sec>\d+(?:\.\d+)?)\s*(?:"|″|”|''|s)?\s*)?
    (?P<{name}_trail>[NSEWnsew])?
"""

_DMS = re.compile(
    rf"^\s*{_DMS_PART.format(name='a')}\s*(?:,|\s)\s*{_DMS_PART.format(name='b')}\s*$",
    re.VERBOSE,
)

LAT_LIMIT = 90.0
LON_LIMIT = 180.0


class Ambiguous(ValueError):
    """A pair that reads as a coordinate but not in the order it was written."""


def parse_coordinates(text: str) -> tuple[float, float] | None:
    """Read "lat, lon" out of pasted text, or return None.

    Latitude first, because that is the order every mapping site writes and the
    order somebody copying from one will paste. A pair the other way round is
    not silently swapped - being taken confidently to the wrong continent is
    worse than being told the input was not understood - but it is recognised,
    and Ambiguous is raised so the interface can say which way round it wants.
    """
    if not text or not text.strip():
        return None

    parsed = _decimal(text) or _dms(text)
    if parsed is None:
        return None

    lat, lon = parsed
    if abs(lat) <= LAT_LIMIT and abs(lon) <= LON_LIMIT:
        return lat, lon

    # Reversed is a real mistake with a clear signature: the first number is
    # impossible as a latitude and the pair works perfectly the other way.
    if abs(lon) <= LAT_LIMIT and abs(lat) <= LON_LIMIT:
        raise Ambiguous(
            f"{lat:g}, {lon:g} is out of range as latitude, longitude. "
            f"Written the other way round it is {lon:g}, {lat:g}."
        )
    return None


def _decimal(text: str) -> tuple[float, float] | None:
    match = _DECIMAL.match(text)
    if match is None:
        return None

    first = _signed(match.group("first"), match.group("first_sign"), match.group("first_suffix"))
    second = _signed(match.group("second"), match.group("second_sign"), match.group("second_suffix"))
    if first is None or second is None:
        return None
    return first, second


def _dms(text: str) -> tuple[float, float] | None:
    match = _DMS.match(text)
    if match is None:
        return None

    values = []
    for name in ("a", "b"):
        degrees = float(match.group(f"{name}_deg"))
        minutes = float(match.group(f"{name}_min") or 0)
        seconds = float(match.group(f"{name}_sec") or 0)
        if minutes >= 60 or seconds >= 60:
            return None

        value = degrees + minutes / 60 + seconds / 3600
        letter = match.group(f"{name}_lead") or match.group(f"{name}_trail")
        if letter and letter.upper() in ("S", "W"):
            value = -value
        values.append(value)

    return values[0], values[1]


def _signed(number: str, before: str | None, after: str | None) -> float | None:
    """Apply a hemisphere letter, refusing a value that carries both signals."""
    value = float(number)
    letter = before or after
    if before and after:
        return None
    if letter is None:
        return value

    # "-15.58338W" says west twice and means it once, but "-15.58338E" is a
    # contradiction and guessing which half was meant would be inventing an
    # answer.
    if letter.upper() in ("S", "W"):
        return -abs(value)
    if value < 0:
        return None
    return value


def coordinate_result(lat: float, lon: float) -> dict[str, object]:
    return {
        "kind": "coordinates",
        "label": f"{lat:.5f}, {lon:.5f}",
        "detail": "Coordinates",
        "lat": lat,
        "lon": lon,
    }


def search(conn: sqlite3.Connection, query: str) -> dict[str, object]:
    """Answer a search. Coordinates for now; pins and tracks are the next part.

    `conn` is unused until then, and taken anyway so that adding those does not
    change the shape of this function or its callers.
    """
    del conn  # not yet: see the module docstring

    text = (query or "").strip()
    if not text:
        return {"query": text, "results": [], "hint": ""}

    try:
        found = parse_coordinates(text)
    except Ambiguous as exc:
        return {"query": text, "results": [], "hint": str(exc)}

    if found is None:
        return {
            "query": text,
            "results": [],
            "hint": (
                "Not a coordinate. Try 27.74367, -15.58338 - decimal degrees, "
                "latitude first. Searching your own pins and tracks is not "
                "built yet."
            ),
        }

    return {"query": text, "results": [coordinate_result(*found)], "hint": ""}
