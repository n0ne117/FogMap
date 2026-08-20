# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plus Codes, which are coordinates written differently.

Open Location Code, Google's, and the reason it fits here is that it needs no
data at all: `8FVC9G8F+6W` decodes to a position with arithmetic, where a postal
code would need a table mapping codes to places. Nothing is looked up and nothing
leaves the machine, which is the same bargain as pasting a latitude and longitude.

Each pair of characters narrows a grid cell, from 20 degrees down to about 14
metres at ten digits, using an alphabet with no vowels in it so a code cannot
accidentally spell a word:

    8F        20°      the 20-degree cell
    8FVC       1°
    8FVC9G     0.05°
    8FVC9G8F   0.0025°   about 275 m
    8FVC9G8F+6W          about 14 m

Short codes are the same thing with the first four characters missing -
`9G8F+6W` - and those have to be recovered from somewhere. The published
algorithm takes a reference position and picks the nearest matching cell, which
means a short code is only unambiguous within about half a degree of that
reference. There is no way to tell a correct recovery from a wrong one: with the
map on Sydney, a Zurich short code resolves to a point near Sydney, confidently
and silently. So the recovered full code is handed back with the answer, for
somebody to recognise or not.
"""

from __future__ import annotations

#: No vowels, so a code cannot spell a word, and no characters that are easily
#: confused with each other when read aloud or written down.
ALPHABET = "23456789CFGHJMPQRVWX"
BASE = len(ALPHABET)

SEPARATOR = "+"
PADDING = "0"

#: Digits before the grid refinement begins. Five pairs, latitude then longitude.
PAIR_DIGITS = 10

#: The grid a refinement digit divides its cell into: five rows, four columns.
GRID_ROWS = 5
GRID_COLUMNS = 4

#: How many digits a full code has before the separator.
PREFIX_DIGITS = 8


class PlusCodeError(ValueError):
    """The text is not a Plus Code, or not one that can be resolved."""


def _digits(code: str) -> str:
    return code.replace(SEPARATOR, "").rstrip(PADDING).upper()


def looks_like(text: str) -> bool:
    """Whether this is worth trying to read as a Plus Code at all.

    Deliberately narrow: exactly one separator, and everything else out of the
    alphabet. A search box takes arbitrary text, and a loose test here would
    claim words that happen to avoid vowels.
    """
    candidate = text.strip().upper()
    if candidate.count(SEPARATOR) != 1:
        return False

    before, after = candidate.split(SEPARATOR)
    if len(after) not in (2, 3):
        return False

    # Two, four, six or eight digits before the separator. Fewer than two is a
    # code that has to be resolved from within about a hundred metres of the
    # answer, which is not a search - refusing it is more use than placing it
    # wherever the map happens to be pointing.
    if len(before) not in (2, 4, 6, PREFIX_DIGITS):
        return False

    body = (before + after).replace(PADDING, "")
    return bool(body) and all(character in ALPHABET for character in body)


def is_full(text: str) -> bool:
    """A code that stands on its own: eight digits before the separator."""
    candidate = text.strip().upper()
    return looks_like(candidate) and len(candidate.split(SEPARATOR)[0]) == PREFIX_DIGITS


def is_short(text: str) -> bool:
    """A code missing its leading digits, which needs somewhere to resolve from."""
    candidate = text.strip().upper()
    return looks_like(candidate) and len(candidate.split(SEPARATOR)[0]) < PREFIX_DIGITS


def decode(code: str) -> tuple[float, float]:
    """The centre of the cell a full code names."""
    if not is_full(code):
        raise PlusCodeError(f"{code!r} is not a full Plus Code.")

    body = _digits(code)
    lat, lon = -90.0, -180.0
    lat_size, lon_size = 20.0, 20.0
    paired = min(len(body), PAIR_DIGITS)

    for index in range(0, paired, 2):
        lat += ALPHABET.index(body[index]) * lat_size
        lon += ALPHABET.index(body[index + 1]) * lon_size
        if index + 2 < paired:
            lat_size /= BASE
            lon_size /= BASE

    for digit in body[PAIR_DIGITS:]:
        value = ALPHABET.index(digit)
        lat_size /= GRID_ROWS
        lon_size /= GRID_COLUMNS
        lat += (value // GRID_COLUMNS) * lat_size
        lon += (value % GRID_COLUMNS) * lon_size

    # The middle of the cell, not its corner: a code names an area, and the
    # middle is the honest single point to stand for it.
    return lat + lat_size / 2, lon + lon_size / 2


def encode(lat: float, lon: float, digits: int = PAIR_DIGITS) -> str:
    """A full code for a position. Used to show what a short code resolved to."""
    lat = min(max(lat, -90.0), 90.0)
    lon = ((lon + 180.0) % 360.0) - 180.0

    remaining_lat, remaining_lon = lat + 90.0, lon + 180.0
    lat_size, lon_size = 20.0, 20.0
    out = []

    for _ in range(min(digits, PAIR_DIGITS) // 2):
        row = min(int(remaining_lat / lat_size), BASE - 1)
        column = min(int(remaining_lon / lon_size), BASE - 1)
        out.append(ALPHABET[row])
        out.append(ALPHABET[column])
        remaining_lat -= row * lat_size
        remaining_lon -= column * lon_size
        lat_size /= BASE
        lon_size /= BASE

    for _ in range(max(0, digits - PAIR_DIGITS)):
        lat_size /= GRID_ROWS
        lon_size /= GRID_COLUMNS
        row = min(int(remaining_lat / lat_size), GRID_ROWS - 1)
        column = min(int(remaining_lon / lon_size), GRID_COLUMNS - 1)
        out.append(ALPHABET[row * GRID_COLUMNS + column])
        remaining_lat -= row * lat_size
        remaining_lon -= column * lon_size

    return f"{''.join(out[:PREFIX_DIGITS])}{SEPARATOR}{''.join(out[PREFIX_DIGITS:])}"


def recover(code: str, reference_lat: float, reference_lon: float) -> tuple[float, float]:
    """Fill in a short code's missing digits from a reference position.

    The published rule: build the prefix by encoding the reference, then step one
    cell if the result landed further than half a cell away. A four-digit
    shortening makes the cell one degree, so the answer is only right if the
    reference is within about half a degree - roughly 55 km at the equator.

    Whether it is right cannot be determined from the code. That is why the
    caller is given the recovered full code to show.
    """
    if not is_short(code):
        raise PlusCodeError(f"{code!r} is not a short Plus Code.")

    candidate = code.strip().upper()
    missing = PREFIX_DIGITS - len(candidate.split(SEPARATOR)[0])
    prefix = encode(reference_lat, reference_lon, missing).replace(SEPARATOR, "")[:missing]

    lat, lon = decode(prefix + candidate)

    cell = float(BASE) ** (2 - missing / 2)
    half = cell / 2
    if reference_lat + half < lat and lat - cell >= -90:
        lat -= cell
    elif reference_lat - half > lat and lat + cell <= 90:
        lat += cell
    if reference_lon + half < lon and lon - cell >= -180:
        lon -= cell
    elif reference_lon - half > lon and lon + cell <= 180:
        lon += cell

    return lat, lon
