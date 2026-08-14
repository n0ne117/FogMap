# SPDX-License-Identifier: AGPL-3.0-or-later
"""TCX parsing.

Garmin's TrainingCenterDatabase format. Element names are matched by local
name, so the several namespace variants in the wild all parse without a
namespace map per vendor.
"""

from __future__ import annotations

from datetime import datetime, timezone

from lxml import etree

from fogmap.ingest.common import Fix, Track


def _local(element: etree._Element, name: str) -> list[etree._Element]:
    return element.findall(f".//{{*}}{name}")


def _first_text(element: etree._Element, name: str) -> str | None:
    found = element.find(f".//{{*}}{name}")
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse(data: bytes | str, filename: str = "upload.tcx") -> list[Track]:
    """Parse TCX bytes into tracks, naming the file in any error raised."""
    payload = data.encode("utf-8") if isinstance(data, str) else data

    parser = etree.XMLParser(recover=False, resolve_entities=False, no_network=True)
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError(f"{filename} is not readable as TCX ({exc}).") from exc

    activities = _local(root, "Activity")
    if not activities:
        raise ValueError(
            f"{filename} parsed as XML but contains no <Activity> elements. "
            "It may be a GPX file with the wrong extension."
        )

    tracks: list[Track] = []

    for activity_index, activity in enumerate(activities):
        sport = activity.get("Sport")
        identifier = _first_text(activity, "Id")
        name = identifier or sport or f"activity {activity_index + 1}"

        fixes: list[Fix] = []
        number = 0

        for trackpoint in _local(activity, "Trackpoint"):
            number += 1

            latitude = _first_text(trackpoint, "LatitudeDegrees")
            longitude = _first_text(trackpoint, "LongitudeDegrees")

            # Indoor trainer points carry a time and a heart rate but no
            # position. Skipping them is correct, not an error.
            if latitude is None and longitude is None:
                continue
            if latitude is None:
                raise ValueError(
                    f"TCX trackpoint {number} has a longitude but no latitude "
                    f"in activity {name!r} of {filename}."
                )
            if longitude is None:
                raise ValueError(
                    f"TCX trackpoint {number} has a latitude but no longitude "
                    f"in activity {name!r} of {filename}."
                )

            try:
                lat_value = float(latitude)
                lon_value = float(longitude)
            except ValueError as exc:
                raise ValueError(
                    f"TCX trackpoint {number} in activity {name!r} of "
                    f"{filename} has an unreadable position "
                    f"({longitude!r}, {latitude!r})."
                ) from exc

            fixes.append(
                Fix(
                    lon=lon_value,
                    lat=lat_value,
                    time=_parse_time(_first_text(trackpoint, "Time")),
                )
            )

        if fixes:
            tracks.append(
                Track(
                    name=name,
                    fixes=fixes,
                    activity=sport,
                    source_id=identifier,
                )
            )

    if not tracks:
        raise ValueError(
            f"{filename} parsed as TCX but contains no positioned trackpoints. "
            "Indoor activities record time without a position and cannot be "
            "mapped."
        )

    return tracks
