# SPDX-License-Identifier: AGPL-3.0-or-later
"""The shared write token.

Irfaran needs one token before anything can be changed. Requiring it to be
invented and pasted into a file before the app will accept a single edit makes
the first five minutes worse for no gain, so one is generated on first start
and kept in the database. Setting IRFARAN_TOKEN in the environment overrides
that, for anyone who would rather manage it themselves.
"""

from __future__ import annotations

import secrets
import sqlite3

from irfaran import settings_env

SETTING_KEY = "api_token"

# Set once the owner confirms they have the token. Until then /api/setup will
# hand a generated token to whoever asks, because on a fresh install there is
# nothing else to prove ownership with and somebody has to be told it. After
# that it is never served again: reads are open, so an endpoint that keeps
# giving out the write token is a lock with the key taped to it.
SETUP_KEY = "setup_completed"
ENV_NAME = "IRFARAN_TOKEN"
ENV_VAR = "TOKEN"


def setup_completed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETUP_KEY,)
    ).fetchone()
    return bool(row and str(row["value"]).strip())


def complete_setup(conn: sqlite3.Connection, when: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETUP_KEY, when),
    )


def revealable(conn: sqlite3.Connection, source: str) -> bool:
    """May the token be shown to whoever is asking?

    Only while setup is unfinished, and only when Irfaran invented it. A token
    from the environment was chosen by the operator, who therefore already has
    it - printing it back would be a leak that buys nothing.
    """
    return source == "generated" and not setup_completed(conn)


def resolve(conn: sqlite3.Connection) -> tuple[str, str]:
    """Return (token, where it came from).

    Source is "environment" when an operator set it, "generated" when Irfaran
    made one up. The distinction matters to the interface: a token someone
    chose is theirs to look after, one Irfaran generated has to be shown at
    least once or it is lost.
    """
    from_env = settings_env.get(ENV_VAR)
    if from_env:
        return from_env, "environment"

    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SETTING_KEY,)
    ).fetchone()
    if row and str(row["value"]).strip():
        return str(row["value"]).strip(), "generated"

    token = secrets.token_hex(24)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (SETTING_KEY, token),
    )
    return token, "generated"
