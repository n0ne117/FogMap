# SPDX-License-Identifier: AGPL-3.0-or-later
"""Workout trackers: services that hold your activities and will hand them over.

Unlike the live sources, nothing is pushed here. Irfaran asks - on a timer, or
when somebody presses the button - which is the right shape for a service that
already has your history and is the authority on it.

Only intervals.icu so far. It is kept behind a small registry rather than
wired in directly, because a second one is likely and the parts that are not
about intervals.icu specifically - is it on, when did it last run, what did it
say - are the same for any of them.

Two decisions worth knowing about:

  activities arrive as sample streams. There is no GPX endpoint - the first
  version of this assumed there was, on the strength of an unauthenticated
  probe where `/activity/{id}.gpx` answered 401 rather than 404. With a real
  key it answers 404: the 401 came from the security filter, not from a route.
  The original upload is available as FIT, which would mean carrying a FIT
  parser, so streams it is. They are JSON and need nothing.

  Positions arrive as two parallel arrays - `data` holds latitudes and `data2`
  longitudes - alongside a `time` stream of offsets in seconds. The listing
  carries `stream_types`, so an activity with no positions is skipped without
  downloading anything.

  they are ingested as `workout`, the same source a file drop uses. Dedup is
  on (source, external_id) and the external id is the segment's own start
  time, so an activity already imported by hand is recognised and skipped
  rather than drawn twice. Giving this its own source name would have made
  every previously imported workout arrive again as a stranger.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterator

from irfaran import db
from irfaran.ingest import common

TRACKERS = ("intervals",)

# The source activities are filed under. Deliberately the same as a file
# import - see the module docstring.
INGEST_SOURCE = "workout"

BASE_URL = "https://intervals.icu/api/v1"

# intervals.icu takes HTTP basic auth with the literal string API_KEY as the
# username and the key as the password.
BASIC_USER = "API_KEY"

USER_AGENT = "Irfaran (+https://github.com/n0ne117/Irfaran)"

# "0" means the authenticated athlete, so nobody has to go and find their id.
DEFAULT_ATHLETE = "0"

DEFAULT_SYNC_HOURS = 12
DEFAULT_SINCE_DAYS = 30

# A first sync on a long history should not fetch a thousand files in one go.
MAX_PER_SYNC = 200

REQUEST_TIMEOUT_S = 60.0


class TrackerError(RuntimeError):
    """Something went wrong, phrased for whoever has to fix the settings."""


def check(name: str) -> str:
    if name not in TRACKERS:
        raise TrackerError(
            f"Unknown workout tracker {name!r}. Irfaran knows "
            f"{', '.join(TRACKERS)}."
        )
    return name


# ------------------------------------------------------------------- settings


def key_of(name: str, field_name: str) -> str:
    return f"{name}_{field_name}"


#: Written to the settings table. The API key is listed in db.SECRET_SETTINGS,
#: so it goes in and is never served back out.
FIELDS = ("enabled", "api_key", "athlete_id", "sync_hours", "since_days")

STATE_FIELDS = ("last_sync", "last_result", "last_error")


def get(conn: sqlite3.Connection, name: str, field_name: str, default: str = "") -> str:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key_of(name, field_name),)
    ).fetchone()
    return str(row["value"]) if row else default


def put(conn: sqlite3.Connection, name: str, field_name: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key_of(name, field_name), value),
    )


def is_enabled(conn: sqlite3.Connection, name: str) -> bool:
    return get(conn, name, "enabled", "false").strip().lower() == "true"


def api_key(conn: sqlite3.Connection, name: str) -> str:
    return get(conn, name, "api_key").strip()


def athlete_id(conn: sqlite3.Connection, name: str) -> str:
    return get(conn, name, "athlete_id", DEFAULT_ATHLETE).strip() or DEFAULT_ATHLETE


def sync_hours(conn: sqlite3.Connection, name: str) -> int:
    """How often to check by itself. Zero means only when asked."""
    try:
        return max(0, int(get(conn, name, "sync_hours", str(DEFAULT_SYNC_HOURS))))
    except ValueError:
        return DEFAULT_SYNC_HOURS


def since_days(conn: sqlite3.Connection, name: str) -> int:
    try:
        return max(1, int(get(conn, name, "since_days", str(DEFAULT_SINCE_DAYS))))
    except ValueError:
        return DEFAULT_SINCE_DAYS


def status(conn: sqlite3.Connection, name: str) -> dict[str, object]:
    """Everything the settings page needs, and never the key itself."""
    check(name)
    return {
        "tracker": name,
        "enabled": is_enabled(conn, name),
        # Whether a key is set, not what it is. The page has to be able to say
        # "configured" without the secret crossing the wire to say it.
        "key_set": bool(api_key(conn, name)),
        "athlete_id": athlete_id(conn, name),
        "sync_hours": sync_hours(conn, name),
        "since_days": since_days(conn, name),
        "last_sync": get(conn, name, "last_sync"),
        "last_result": get(conn, name, "last_result"),
        "last_error": get(conn, name, "last_error"),
        "due": is_due(conn, name),
    }


def is_due(conn: sqlite3.Connection, name: str, now: datetime | None = None) -> bool:
    """Has enough time passed for the timer to want another look?"""
    if not is_enabled(conn, name) or not api_key(conn, name):
        return False

    hours = sync_hours(conn, name)
    if hours == 0:
        return False

    last = get(conn, name, "last_sync")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - when >= timedelta(hours=hours)


# --------------------------------------------------------------------- client


def _auth_header(key: str) -> str:
    raw = f"{BASIC_USER}:{key}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def fetch(path: str, key: str, accept: str = "application/json") -> bytes:
    """One authenticated GET against intervals.icu.

    Split out so tests can replace the transport without a network, and so
    every request carries the same auth and timeout.
    """
    request = urllib.request.Request(
        f"{BASE_URL}{path}",
        headers={
            "Authorization": _auth_header(key),
            "Accept": accept,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise TrackerError(
                "intervals.icu refused the API key. Check it under Data "
                "sources, Workout trackers - it is the one from Settings, "
                "Developer on intervals.icu."
            ) from exc
        if exc.code == 404:
            raise TrackerError(f"intervals.icu has nothing at {path}.") from exc
        raise TrackerError(
            f"intervals.icu answered {exc.code} for {path}."
        ) from exc
    except urllib.error.URLError as exc:
        raise TrackerError(
            f"Could not reach intervals.icu: {exc.reason}. If this server has "
            "no way out to the internet, a workout tracker cannot work."
        ) from exc


def list_activities(
    key: str, athlete: str, oldest: date, newest: date | None = None
) -> list[dict[str, object]]:
    """Activities in a date range, newest first.

    `oldest` is required by the API - leaving it off is a 422, not a default of
    all time - so the caller always decides how far back to look.
    """
    query = f"?oldest={oldest.isoformat()}"
    if newest is not None:
        query += f"&newest={newest.isoformat()}"

    raw = fetch(f"/athlete/{athlete}/activities{query}", key)
    try:
        listed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrackerError(
            "intervals.icu returned something that is not JSON for the "
            "activity list."
        ) from exc

    if not isinstance(listed, list):
        raise TrackerError(
            "intervals.icu returned an activity list that is not a list."
        )
    return [item for item in listed if isinstance(item, dict)]


def download_streams(key: str, activity_id: str) -> dict[str, dict[str, object]]:
    """The sample streams for one activity, keyed by type.

    Not GPX. There is no GPX endpoint - `/activity/{id}.gpx` answers 404 with a
    real key, and the 401 it gives without one comes from the security filter
    rather than from a route that exists. The original upload is available as
    FIT, which would mean a FIT parser; streams are JSON and need nothing.
    """
    raw = fetch(f"/activity/{activity_id}/streams", key)
    try:
        listed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TrackerError(
            f"intervals.icu returned something that is not JSON for the "
            f"streams of activity {activity_id}."
        ) from exc

    if not isinstance(listed, list):
        raise TrackerError(
            f"intervals.icu returned streams for {activity_id} that are not a list."
        )
    return {
        str(item.get("type")): item
        for item in listed
        if isinstance(item, dict) and item.get("type")
    }


def has_gps(activity: dict[str, object]) -> bool:
    """Does the listing already say this activity has positions?

    Every activity carries `stream_types`, so an indoor session can be skipped
    without downloading anything at all - which is most of a winter, and the
    difference between a sync that fetches five files and one that fetches
    fifty.
    """
    types = activity.get("stream_types")
    return isinstance(types, list) and "latlng" in types


def started_at(activity: dict[str, object]) -> datetime | None:
    """When the activity began, as an aware datetime.

    The sample streams carry offsets in seconds, not timestamps, so without
    this there is nothing to hang them on - and the dedup key is the first
    fix's time, which is what lets an activity already imported from a file be
    recognised rather than drawn twice.
    """
    for field_name in ("start_date", "start_date_local"):
        raw = activity.get(field_name)
        if not isinstance(raw, str) or not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return None


def track_from_streams(
    activity: dict[str, object], streams: dict[str, dict[str, object]]
) -> common.Track | None:
    """Turn latitude, longitude and time streams into a track.

    intervals.icu splits a position into two parallel arrays: `data` holds the
    latitudes and `data2` the longitudes. Samples where either is null are GPS
    dropouts and are left out rather than drawn as a line through nowhere.
    """
    latlng = streams.get("latlng")
    if not latlng or latlng.get("allNull"):
        return None

    lats = latlng.get("data")
    lons = latlng.get("data2")
    if not isinstance(lats, list) or not isinstance(lons, list):
        return None

    offsets = streams.get("time", {}).get("data")
    offsets = offsets if isinstance(offsets, list) else []
    begin = started_at(activity)

    fixes: list[common.Fix] = []
    for index in range(min(len(lats), len(lons))):
        lat, lon = lats[index], lons[index]
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue

        when = None
        if begin is not None:
            # One second per sample when there is no time stream, rather than
            # one timestamp for all of them: identical times would look like a
            # single instant, and a second apart is well inside the gap
            # threshold that splits a track into segments.
            offset = offsets[index] if index < len(offsets) else index
            if not isinstance(offset, (int, float)):
                offset = index
            when = begin + timedelta(seconds=float(offset))

        fixes.append(common.Fix(lon=float(lon), lat=float(lat), time=when))

    if not fixes:
        return None

    return common.Track(
        name=str(activity.get("name") or "intervals.icu activity"),
        fixes=fixes,
        activity=str(activity.get("type") or "") or None,
        source_id=str(activity.get("id") or "") or None,
    )


# ----------------------------------------------------------------------- sync


@dataclass
class SyncResult:
    looked_at: int = 0
    imported: int = 0
    already_here: int = 0
    no_gps: int = 0
    failed: int = 0
    events: int = 0
    tiles: set[tuple[int, int]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "looked_at": self.looked_at,
            "imported": self.imported,
            "already_here": self.already_here,
            "no_gps": self.no_gps,
            "failed": self.failed,
            "events": self.events,
            "tiles_touched": len(self.tiles),
            "notes": self.notes[:10],
        }

    def summary(self) -> str:
        if self.imported:
            text = f"{self.imported} new"
            if self.already_here:
                text += f", {self.already_here} already here"
        elif self.looked_at:
            text = f"nothing new in {self.looked_at} activities"
        else:
            text = "no activities in that window"
        if self.no_gps:
            text += f", {self.no_gps} without GPS"
        if self.failed:
            text += f", {self.failed} failed"
        return text


def oldest_to_ask_for(conn: sqlite3.Connection, name: str) -> date:
    """How far back this sync should look.

    A first run uses the configured window. After that it goes back a couple of
    days past the last successful sync rather than to the last sync exactly: an
    activity can be uploaded to intervals.icu long after it happened, and a
    window that starts at the last check would never see it.
    """
    last = get(conn, name, "last_sync")
    window = timedelta(days=since_days(conn, name))
    if last:
        try:
            when = datetime.fromisoformat(last)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return (when - timedelta(days=2)).date()
        except ValueError:
            pass
    return (datetime.now(timezone.utc) - window).date()


def precheck(conn: sqlite3.Connection, name: str, force: bool = False) -> str:
    """Everything that must be true before a sync is worth starting.

    Separate from the sync itself because the endpoint streams its progress,
    and a generator does not run - so does not raise - until something pulls on
    it. A missing key has to be a plain refusal with a status code, not an
    error line arriving inside a 200.
    """
    check(name)
    key = api_key(conn, name)
    if not key:
        raise TrackerError(
            "No intervals.icu API key set. Data sources, Workout trackers."
        )
    if not force and not is_enabled(conn, name):
        raise TrackerError(
            f"The {name} tracker is switched off. Turn it on under Data "
            "sources, Workout trackers."
        )
    return key


def sync_iter(
    conn: sqlite3.Connection,
    name: str = "intervals",
    *,
    force: bool = False,
    result: SyncResult | None = None,
) -> Iterator[dict[str, object]]:
    """Fetch what is new, reporting each activity as it lands.

    Yields progress a caller can show. Downloading fifty activities is a minute
    or more of work, and a minute of nothing is indistinguishable from a hang -
    which is also long enough for a reverse proxy to give up on a request that
    has sent no bytes, so this is not only about manners.

    `result` is filled in as it goes, so a caller that wants the totals rather
    than the commentary can hand one in and ignore what is yielded. Same shape
    as composite.render_views_iter.

    Nothing is rendered here. Every activity that lands adds to the pending
    render queue and one render is asked for at the end, because rendering per
    activity turned a three-file import into ten seconds of work back when
    imports did that.
    """
    key = precheck(conn, name, force)
    result = result if result is not None else SyncResult()
    started = datetime.now(timezone.utc)

    yield {"stage": "listing"}

    activities = list_activities(key, athlete_id(conn, name), oldest_to_ask_for(conn, name))
    result.looked_at = len(activities)
    batch = activities[:MAX_PER_SYNC]

    yield {"stage": "listed", "found": len(activities), "total": len(batch)}

    for index, activity in enumerate(batch, start=1):
        if activity.get("id") in (None, ""):
            result.failed += 1
        else:
            _take_one(conn, key, activity, result)

        yield {
            "stage": "activity",
            "done": index,
            "total": len(batch),
            "name": str(activity.get("name") or activity.get("type") or ""),
            "imported": result.imported,
            "already_here": result.already_here,
            "no_gps": result.no_gps,
            "failed": result.failed,
        }

    if len(activities) > MAX_PER_SYNC:
        result.notes.append(
            f"Stopped after {MAX_PER_SYNC} activities. Sync again to continue."
        )

    if result.tiles:
        db.defer_render(conn, result.tiles)

    put(conn, name, "last_sync", started.isoformat(timespec="seconds"))
    put(conn, name, "last_result", result.summary())
    put(conn, name, "last_error", "")

    yield {
        "stage": "done",
        "finished": True,
        "summary": result.summary(),
        **result.as_dict(),
    }


def _take_one(
    conn: sqlite3.Connection,
    key: str,
    activity: dict[str, object],
    result: SyncResult,
) -> None:
    """One activity, from download to rasterised. Never raises.

    One awkward activity must not end a sync - the rest of the archive is still
    worth having.
    """
    identifier = str(activity.get("id"))

    if not has_gps(activity):
        result.no_gps += 1
        return

    try:
        streams = download_streams(key, identifier)
    except TrackerError as exc:
        result.failed += 1
        result.notes.append(f"{identifier}: {exc}")
        return

    try:
        track = track_from_streams(activity, streams)
    except Exception as exc:  # noqa: BLE001
        result.failed += 1
        result.notes.append(f"{identifier}: unreadable streams ({exc})")
        return

    if track is None:
        result.no_gps += 1
        return

    try:
        ingested = common.ingest_tracks(conn, INGEST_SOURCE, [track])
    except Exception as exc:  # noqa: BLE001
        result.failed += 1
        result.notes.append(f"{identifier}: {exc}")
        return

    if ingested.events_created:
        result.imported += 1
        result.events += ingested.events_created
    else:
        result.already_here += 1
    result.tiles |= set(ingested.tiles_touched)


def sync(
    conn: sqlite3.Connection, name: str = "intervals", *, force: bool = False
) -> SyncResult:
    """Run a whole sync and hand back the totals. Safe to repeat.

    The timer uses this; the endpoint uses sync_iter so it can report as it
    goes. Nothing is deleted and nothing is overwritten - a sync adds what is
    not already here, and running it twice changes nothing the second time.
    """
    result = SyncResult()
    for _ in sync_iter(conn, name, force=force, result=result):
        pass
    return result
