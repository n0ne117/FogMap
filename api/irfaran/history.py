# SPDX-License-Identifier: AGPL-3.0-or-later
"""What happened: the History tab's source of truth.

Deliberately not the event log. That records what entered the map, which is a
different question - it cannot express a failure, a render, or a setting being
changed, and those are most of what somebody looks at a history for.

It is also the only place an error is kept. Before this, a failed import
existed in the container's stdout and nowhere else: a restart discarded it and
the interface could not read it, so the answer to "why is that track missing"
was always "look in the logs on the server".

Four categories, which are what the interface colours by:

  error   something did not work
  manual  something you did by hand - a file you imported, a stroke you drew
  source  something that arrived on its own - a tracker, a live feed
  system  everything else - renders, settings, first-run

Kept small on purpose. This lives in the database that gets backed up and
carried between instances, so it is capped by both age and count, and a live
source delivering every few minutes coalesces into one line that grows rather
than three hundred lines a day that push out everything worth reading.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

CATEGORIES = ("error", "manual", "source", "system")

#: Keep this many entries at most, and nothing older than this.
MAX_ENTRIES = 2000
MAX_AGE_DAYS = 90

#: A repeat of the same thing within this long folds into the previous line.
COALESCE_MINUTES = 15


class HistoryError(ValueError):
    """A bad category, which is a programming mistake rather than bad input."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(
    conn: sqlite3.Connection,
    category: str,
    action: str,
    message: str,
    detail: dict[str, object] | None = None,
    *,
    coalesce: bool = False,
) -> None:
    """Write one line of history. Never raises on a full or busy database.

    Recording history must not be able to break the thing it is recording, so
    every failure here is swallowed. A missing line in the History tab is a
    nuisance; an import that fails because writing a log line failed is not.

    `coalesce` folds a repeat of the same action into the previous line, for
    sources that deliver continuously.
    """
    if category not in CATEGORIES:
        raise HistoryError(
            f"Unknown history category {category!r}. Valid: {', '.join(CATEGORIES)}."
        )

    try:
        if coalesce and _fold_into_previous(conn, category, action, message):
            return

        conn.execute(
            "INSERT INTO log (at, category, action, message, count, detail) "
            "VALUES (?, ?, ?, ?, 1, ?)",
            (now(), category, action, message, json.dumps(detail) if detail else None),
        )
        _prune(conn)
    except sqlite3.Error:
        # Including a locked database. The history is the least important
        # writer here and has no business winning a fight for the lock.
        pass


def _fold_into_previous(
    conn: sqlite3.Connection, category: str, action: str, message: str
) -> bool:
    """Bump the newest line if it is the same thing, recently."""
    row = conn.execute(
        "SELECT id, at, count FROM log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return False

    same = conn.execute(
        "SELECT category, action FROM log WHERE id = ?", (row["id"],)
    ).fetchone()
    if same["category"] != category or same["action"] != action:
        return False

    try:
        when = datetime.fromisoformat(str(row["at"]))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - when > timedelta(minutes=COALESCE_MINUTES):
        return False

    conn.execute(
        "UPDATE log SET count = count + 1, at = ?, message = ? WHERE id = ?",
        (now(), message, row["id"]),
    )
    return True


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    ).isoformat(timespec="seconds")
    conn.execute("DELETE FROM log WHERE at < ?", (cutoff,))
    conn.execute(
        "DELETE FROM log WHERE id NOT IN "
        "(SELECT id FROM log ORDER BY id DESC LIMIT ?)",
        (MAX_ENTRIES,),
    )


def recent(
    conn: sqlite3.Connection, limit: int = 200, category: str | None = None
) -> list[dict[str, object]]:
    """The newest entries first, optionally of one category."""
    limit = max(1, min(int(limit), MAX_ENTRIES))
    if category is not None and category not in CATEGORIES:
        raise HistoryError(
            f"Unknown history category {category!r}. Valid: {', '.join(CATEGORIES)}."
        )

    sql = "SELECT id, at, category, action, message, count, detail FROM log"
    params: list[object] = []
    if category:
        sql += " WHERE category = ?"
        params.append(category)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    entries = []
    for row in conn.execute(sql, params):
        detail = None
        if row["detail"]:
            try:
                detail = json.loads(row["detail"])
            except json.JSONDecodeError:
                detail = None
        entries.append(
            {
                "id": int(row["id"]),
                "at": str(row["at"]),
                "category": str(row["category"]),
                "action": str(row["action"]),
                "message": str(row["message"]),
                "count": int(row["count"]),
                "detail": detail,
            }
        )
    return entries


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many of each category are being kept."""
    found = {category: 0 for category in CATEGORIES}
    for row in conn.execute("SELECT category, COUNT(*) AS n FROM log GROUP BY category"):
        if str(row["category"]) in found:
            found[str(row["category"])] = int(row["n"])
    return found


def clear(conn: sqlite3.Connection) -> int:
    """Forget everything. Returns how many entries went."""
    row = conn.execute("SELECT COUNT(*) AS n FROM log").fetchone()
    conn.execute("DELETE FROM log")
    return int(row["n"]) if row else 0
