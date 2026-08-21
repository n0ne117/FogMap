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

import json
import re
import sqlite3

from irfaran import gazetteer, pluscode

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

#: What the bar is allowed to look through, and what each is called on screen.
KINDS = {
    "pins": "search_pins",
    "tracks": "search_tracks",
    "coordinates": "search_coordinates",
    "plus_codes": "search_plus_codes",
    "place_names": "search_place_names",
    "pois": "search_pois",
}

#: How each reads in a sentence, since "Plus_codes are switched off" does not.
NAMES = {
    "pins": "Pins",
    "tracks": "Tracks",
    "coordinates": "Coordinates",
    "plus_codes": "Plus Codes",
    "place_names": "Place names",
    "pois": "Points of interest",
}

DEFAULTS = {
    "pins": True,
    "tracks": False,
    "coordinates": True,
    "plus_codes": False,
    "place_names": False,
    "pois": False,
}


def included(conn: sqlite3.Connection) -> dict[str, bool]:
    """Which kinds of result are switched on.

    Absent means on for pins and coordinates and off for tracks, matching the
    seeded defaults - a database from before these settings existed should
    behave like a fresh one rather than like everything switched off.
    """
    stored = {
        str(row["key"]): str(row["value"]).strip().lower()
        for row in conn.execute(
            "SELECT key, value FROM settings WHERE key LIKE 'search\\_%' ESCAPE '\\'"
        )
    }
    return {
        kind: stored.get(key, str(DEFAULTS[kind]).lower()) == "true"
        for kind, key in KINDS.items()
    }


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


def plus_code_result(
    code: str, lat: float, lon: float, recovered: bool
) -> dict[str, object]:
    """One Plus Code, said back as the full code it resolved to.

    A short code cannot be checked: with the map on Sydney, a Zurich code
    resolves near Sydney, confidently. Handing back the full code is what makes
    a wrong recovery visible to somebody who knows where they meant.
    """
    return {
        "kind": "pluscode",
        "label": code,
        "detail": (
            "Short Plus Code — resolved from where the map is looking"
            if recovered
            else "Plus Code"
        ),
        "lat": lat,
        "lon": lon,
    }


def coordinate_result(lat: float, lon: float) -> dict[str, object]:
    return {
        "kind": "coordinates",
        "label": f"{lat:.5f}, {lon:.5f}",
        "detail": "Coordinates",
        "lat": lat,
        "lon": lon,
    }


#: How many results to answer with. A search box is for finding one thing, and
#: a list longer than this is a list nobody reads to the end of.
LIMIT = 20

#: A bare year, which is the only date a track can be searched by.
#:
#: Nothing finer is recorded. `created_at` on an event is the moment it was
#: imported - `datetime.now()` in the ingest, for every source - so matching a
#: month against it would answer "you were there in August 2026" about a ride
#: somebody uploaded in August 2026 and rode years earlier. The year in `layers`
#: is derived from the fixes' own timestamps, so that one is true.
_YEAR = re.compile(r"^\d{4}$")

#: A finer date than can be answered. Only used to explain why.
_FINER = re.compile(r"^\d{4}-\d{2}(?:-\d{2})?$")


def _matches(needle: str, *haystacks: object) -> bool:
    """Case-insensitive contains, done in Python rather than in SQL.

    SQLite's LIKE and lower() only fold ASCII, so `dörfl` would not find
    `Dörfl` and `wien` would not find `Wien` in any word carrying an umlaut -
    which in this archive is most of them. casefold() is what actually works,
    and the sets being matched here are small enough that reading them into
    Python costs nothing worth counting.
    """
    folded = needle.casefold()
    for hay in haystacks:
        if hay is None:
            continue
        if folded in str(hay).casefold():
            return True
    return False


def _pins(conn: sqlite3.Connection, text: str) -> list[dict[str, object]]:
    """Pins by title, tag, label, folder or category."""
    rows = conn.execute(
        """
        SELECT p.id, p.name, p.category, p.tags, p.lat, p.lon, p.people,
               l.name AS label, f.name AS folder
        FROM places p
        LEFT JOIN labels l ON l.id = p.label_id
        LEFT JOIN folders f ON f.id = p.folder_id
        ORDER BY p.name COLLATE NOCASE
        """
    ).fetchall()

    found = []
    for row in rows:
        tags = _tag_words(row["tags"])
        people = _tag_words(row["people"])
        if not _matches(
            text,
            row["name"],
            row["category"],
            row["label"],
            row["folder"],
            " ".join(tags),
            # Who was there is a thing somebody deliberately recorded, so it is
            # a thing worth finding a pin by.
            " ".join(people),
        ):
            continue

        # Why it matched, said in the row rather than left to be guessed at.
        because = [
            part for part in (row["label"], row["folder"], *tags, *people) if part
        ]
        found.append(
            {
                "kind": "pin",
                "id": int(row["id"]),
                "label": str(row["name"]),
                "detail": "Pin" + (f" — {', '.join(because[:3])}" if because else ""),
                "lat": float(row["lat"]),
                "lon": float(row["lon"]),
            }
        )

    # Whole-word or leading matches first: somebody typing "cao" wants Caorle
    # before a pin that merely mentions it in a tag.
    folded = text.casefold()
    found.sort(key=lambda hit: (not str(hit["label"]).casefold().startswith(folded), str(hit["label"]).casefold()))
    return found


def _tag_words(raw: object) -> list[str]:
    if not raw:
        return []
    try:
        loaded = json.loads(str(raw))
    except (TypeError, ValueError):
        return []
    return [str(part) for part in loaded] if isinstance(loaded, list) else []


def _tracks(conn: sqlite3.Connection, text: str) -> tuple[list[dict[str, object]], int]:
    """Tracks by name, or by the year they belong to.

    Grouped by name, because one imported file becomes as many events as it has
    gaps in it - a single 827 km ride is 37 of them here - and a search that
    answered with each segment separately would bury everything else.

    The year comes from `layers`, not from `created_at`: created_at is when the
    file was imported, which for anything pre-dating GPS is the day somebody got
    round to drawing it rather than the day they were there.

    Two passes, and the reason is measured. Matching needs names, layers and
    dates; only the results being returned need geometry, and geometry is where
    the bytes are. Reading it for every candidate meant 27.8 MB a search on this
    archive - cost set by how much has been walked rather than by what was asked
    for, which is the shape of the scan that made every render slow before
    0.17.6. Names first, geometry for the twenty that survive.

    Returns the results and how many tracks matched in total, so the interface
    can say it is showing a slice.
    """
    year = text if _YEAR.match(text) else None
    rows = conn.execute(
        """
        SELECT id, json_extract(meta, '$.track') AS name, layers, created_at
        FROM events
        WHERE json_extract(meta, '$.track') IS NOT NULL
        ORDER BY id DESC
        """
    ).fetchall()

    groups: dict[str, dict[str, object]] = {}
    for row in rows:
        name = str(row["name"])
        # A year matches what is filed under it, and a name still matches by
        # name - live sources name their tracks after a timestamp, so "2024-12"
        # finds those the ordinary way.
        by_year = year is not None and year in _years(row["layers"])
        if not by_year and not _matches(text, name):
            continue

        group = groups.setdefault(name, {"ids": [], "years": set(), "newest": ""})
        group["ids"].append(int(row["id"]))  # type: ignore[union-attr]
        group["years"].update(_years(row["layers"]))  # type: ignore[union-attr]
        group["newest"] = max(str(group["newest"]), str(row["created_at"] or ""))

    ordered = sorted(
        groups.items(), key=lambda pair: str(pair[1]["newest"]), reverse=True
    )

    found = []
    for name, group in ordered:
        if len(found) >= LIMIT:
            break

        box = _bounds(conn, list(group["ids"]))  # type: ignore[arg-type]
        if box is None:
            continue  # no usable geometry, so nowhere to go

        west, south, east, north = box
        years = sorted(str(year) for year in group["years"])  # type: ignore[union-attr]
        pieces = len(list(group["ids"]))  # type: ignore[arg-type]

        detail = "Track"
        if years:
            detail += f" — {years[0]}" if len(years) == 1 else f" — {years[0]}–{years[-1]}"
        if pieces > 1:
            detail += f", {pieces} segments"

        found.append(
            {
                "kind": "track",
                "label": name,
                "detail": detail,
                "lat": (south + north) / 2,
                "lon": (west + east) / 2,
                "bounds": [[west, south], [east, north]],
            }
        )

    return found, len(groups)


#: How many ids to name in one query. SQLite refuses an expression tree deeper
#: than a thousand, which a long track's segment list can reach on its own.
IDS_PER_QUERY = 400


def _bounds(
    conn: sqlite3.Connection, ids: list[int]
) -> tuple[float, float, float, float] | None:
    """The box one track covers, read only for a track being returned."""
    west, south, east, north = 180.0, 90.0, -180.0, -90.0
    seen = False

    for start in range(0, len(ids), IDS_PER_QUERY):
        batch = ids[start : start + IDS_PER_QUERY]
        placeholders = ",".join("?" * len(batch))
        for row in conn.execute(
            f"SELECT geometry FROM events WHERE id IN ({placeholders})", batch
        ):
            try:
                shape = json.loads(str(row["geometry"]))
            except (TypeError, ValueError):
                continue
            for lon, lat in _points(shape.get("coordinates"), shape.get("type")):
                seen = True
                west, east = min(west, lon), max(east, lon)
                south, north = min(south, lat), max(north, lat)

    return (west, south, east, north) if seen else None


def _years(raw: object) -> set[str]:
    try:
        loaded = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return set()
    return {str(part) for part in loaded if str(part).isdigit()} if isinstance(loaded, list) else set()


def _points(coordinates: object, kind: object) -> list[tuple[float, float]]:
    if kind == "Point":
        pair = coordinates if isinstance(coordinates, list) else []
        return [(float(pair[0]), float(pair[1]))] if len(pair) >= 2 else []
    if kind == "LineString":
        rings = [coordinates]
    elif kind == "Polygon":
        rings = coordinates if isinstance(coordinates, list) else []
    else:
        return []

    out = []
    for ring in rings:
        if not isinstance(ring, list):
            continue
        for pair in ring:
            if isinstance(pair, list) and len(pair) >= 2:
                out.append((float(pair[0]), float(pair[1])))
    return out


def _plus_code(
    text: str, on: dict[str, bool], reference: tuple[float, float] | None
) -> dict[str, object] | None:
    """A Plus Code result, or a hint saying why there is not one."""
    if pluscode.is_full(text):
        if not on["plus_codes"]:
            return {"hint": _switched_off("plus_codes")}
        lat, lon = pluscode.decode(text)
        return {"result": plus_code_result(text.strip().upper(), lat, lon, False)}

    if not pluscode.is_short(text):
        return None

    # One setting for both forms. A short code was its own toggle because it is
    # resolved from wherever the map is looking and so can be confidently wrong -
    # but "use where I am looking" is what the search bar's own switch says, and
    # saying it twice in two places is one place too many. The recovered full
    # code in the answer is what makes a wrong resolution visible.
    if not on["plus_codes"]:
        return {"hint": _switched_off("plus_codes")}
    if reference is None:
        return {
            "hint": (
                "A short Plus Code has to be resolved from somewhere, and the "
                "map did not say where it is looking. Move the map near the "
                "place, or paste the full code."
            )
        }

    lat, lon = pluscode.recover(text, *reference)
    return {"result": plus_code_result(pluscode.encode(lat, lon), lat, lon, True)}


def _listed(names: list[str]) -> str:
    """"A", "A and B", "A, B and C" - rather than "A and B and C"."""
    if len(names) <= 2:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _within(hit: dict[str, object], box: tuple[float, float, float, float]) -> bool:
    """Whether a result is inside the map's current view."""
    west, south, east, north = box
    lat, lon = float(hit["lat"]), float(hit["lon"])  # type: ignore[arg-type]
    return south <= lat <= north and west <= lon <= east


def _switched_off(kind: str) -> str:
    return f"{NAMES[kind]} are switched off under Settings, Search."


def search(
    conn: sqlite3.Connection,
    query: str,
    reference: tuple[float, float] | None = None,
    inside: tuple[float, float, float, float] | None = None,
) -> dict[str, object]:
    """Answer a search: a coordinate, or the pins and tracks it matches.

    Read-only throughout, which is why it needs no token. Seeing where you have
    been is what the map already shows; keeping a searched coordinate as a pin
    is a write, and that is where credentials start to matter - so the interface
    offers that only when it has them.
    """
    text = (query or "").strip()
    if not text:
        return {"query": text, "results": [], "hint": ""}

    on = included(conn)

    # A Plus Code cannot be mistaken for anything else - its alphabet has no
    # vowels and it carries a separator - so it is answered before the text
    # search rather than alongside it.
    coded = _plus_code(text, on, reference)
    if coded is not None:
        if "result" in coded:
            return {"query": text, "results": [coded["result"]], "hint": ""}
        return {"query": text, "results": [], "hint": str(coded["hint"])}

    if on["coordinates"]:
        try:
            found = parse_coordinates(text)
        except Ambiguous as exc:
            return {"query": text, "results": [], "hint": str(exc)}

        if found is not None:
            return {"query": text, "results": [coordinate_result(*found)], "hint": ""}

    pins = _pins(conn, text) if on["pins"] else []
    if inside is not None:
        pins = [hit for hit in pins if _within(hit, inside)]

    tracks, track_total = _tracks(conn, text) if on["tracks"] else ([], 0)
    if inside is not None:
        tracks = [hit for hit in tracks if _within(hit, inside)]
        track_total = len(tracks)

    # The basemap's own names, if they have been extracted. Last, because a pin
    # somebody placed themselves is a better answer than a label off a map.
    wanted = [kind for kind in ("place_names", "pois") if on[kind]]
    named = gazetteer.look_up(
        conn,
        text,
        ["place" if kind == "place_names" else "poi" for kind in wanted],
        LIMIT,
        inside,
    ) if wanted else []

    results = [*pins, *tracks, *named]
    total = len(pins) + track_total + len(named)

    # Nothing found and something switched off is not the same as nothing to
    # find. Saying which, rather than leaving somebody to wonder why a track
    # they can see on the map cannot be searched for.
    # Only the two kinds a plain word could have matched. Plus Codes are
    # answered above, and the basemap's own names are usually off because they
    # have not been read out of the archive rather than because somebody
    # switched them off - so listing them here would be noise about things
    # nobody was asking for, and misleading noise at that.
    excluded = [kind for kind in ("pins", "tracks") if not on[kind]]

    hint = ""
    if not results and excluded:
        hint = (
            f"Nothing here matches {text!r}. "
            f"{_listed([NAMES[kind] for kind in excluded])} "
            # Always "are": every one of these names is a plural.
            "are switched off under Settings, Search."
        )
    elif not results and _FINER.match(text):
        hint = (
            f"Nothing matches {text!r}. Only the year of a track is recorded, "
            f"so try {text[:4]} - the rest of a date is not kept, because what "
            "an event stores is when it was imported rather than when it "
            "happened."
        )
    elif not results:
        hint = (
            f"Nothing here matches {text!r}. Pins and tracks are searched by "
            "name, and tracks also by year - a coordinate like "
            "27.74367, -15.58338 goes straight there."
        )
    elif total > LIMIT:
        hint = f"Showing {LIMIT} of {total} matches."

    return {"query": text, "results": results[:LIMIT], "hint": hint}
