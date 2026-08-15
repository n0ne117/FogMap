# SPDX-License-Identifier: AGPL-3.0-or-later
"""Labels and folders: the two things places are sorted by.

Neither carries any location. A label is what a pin looks like, a folder is
where it is filed, and both exist so that a few hundred pins remain something
a person can find their way around rather than a field of identical dots.
"""

from __future__ import annotations

import sqlite3

from irfaran import composite

DEFAULT_COLOUR = "#4d8fd6"

# How deep folders may nest.
#
# Two levels is what was asked for and roughly what a sidebar can show without
# horizontal scrolling. The column would allow any depth; the limit is here so
# that a tree nobody can read is refused at the point it is created rather than
# discovered later.
MAX_DEPTH = 2


class OrganiseError(ValueError):
    """Bad input, phrased for whoever typed it."""


def _name(payload: dict, noun: str) -> str:
    name = str(payload.get("name", "")).strip()
    if not name:
        raise OrganiseError(f"A {noun} needs a name.")
    if len(name) > 120:
        raise OrganiseError(
            f"That {noun} name is {len(name)} characters. Keep it under 120."
        )
    return name


# ------------------------------------------------------------------- labels


def label_as_dict(row: sqlite3.Row) -> dict[str, object]:
    return {"id": row["id"], "name": row["name"], "colour": row["colour"]}


def labels(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM labels ORDER BY name COLLATE NOCASE")
    return [label_as_dict(row) for row in rows]


def create_label(conn: sqlite3.Connection, payload: dict) -> dict[str, object]:
    name = _name(payload, "label")
    colour = _colour(payload.get("colour"))

    try:
        cursor = conn.execute(
            "INSERT INTO labels (name, colour) VALUES (?, ?)", (name, colour)
        )
    except sqlite3.IntegrityError:
        raise OrganiseError(f"There is already a label called {name!r}.") from None

    row = conn.execute(
        "SELECT * FROM labels WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return label_as_dict(row)


def update_label(
    conn: sqlite3.Connection, label_id: int, payload: dict
) -> dict[str, object]:
    existing = conn.execute(
        "SELECT * FROM labels WHERE id = ?", (label_id,)
    ).fetchone()
    if existing is None:
        raise KeyError(label_id)

    name = _name({**label_as_dict(existing), **payload}, "label")
    colour = _colour(payload.get("colour", existing["colour"]))

    try:
        conn.execute(
            "UPDATE labels SET name = ?, colour = ? WHERE id = ?",
            (name, colour, label_id),
        )
    except sqlite3.IntegrityError:
        raise OrganiseError(f"There is already a label called {name!r}.") from None

    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    return label_as_dict(row)


def delete_label(conn: sqlite3.Connection, label_id: int) -> dict[str, object]:
    row = conn.execute("SELECT * FROM labels WHERE id = ?", (label_id,)).fetchone()
    if row is None:
        raise KeyError(label_id)

    # Places keep their pin, they just lose the colour. Deleting a label is
    # not a reason to lose somewhere you have been.
    conn.execute("UPDATE places SET label_id = NULL WHERE label_id = ?", (label_id,))
    conn.execute("DELETE FROM labels WHERE id = ?", (label_id,))
    return label_as_dict(row)


def _colour(raw: object) -> str:
    if raw in (None, ""):
        return DEFAULT_COLOUR
    try:
        return composite.to_hex(composite.parse_colour(str(raw), "A label colour"))
    except ValueError as exc:
        raise OrganiseError(str(exc)) from None


# ------------------------------------------------------------------ folders


def folder_as_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "name": row["name"],
        "parent_id": row["parent_id"],
        "visible": bool(row["visible"]),
    }


def folders(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT * FROM folders ORDER BY parent_id IS NOT NULL, name COLLATE NOCASE"
    )
    return [folder_as_dict(row) for row in rows]


def _parent(conn: sqlite3.Connection, value: object) -> int | None:
    """Check a parent exists and that nesting under it stays inside MAX_DEPTH."""
    if value in (None, "", "none"):
        return None
    try:
        parent_id = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise OrganiseError(f"parent_id must be a number, got {value!r}.") from None

    row = conn.execute(
        "SELECT * FROM folders WHERE id = ?", (parent_id,)
    ).fetchone()
    if row is None:
        raise OrganiseError(f"There is no folder with id {parent_id}.")

    if depth_of(conn, parent_id) + 1 >= MAX_DEPTH:
        raise OrganiseError(
            f"Folders nest {MAX_DEPTH} deep. {row['name']!r} is already as far "
            "in as they go."
        )
    return parent_id


def depth_of(conn: sqlite3.Connection, folder_id: int) -> int:
    """How many folders are above this one. A top-level folder is 0."""
    depth = 0
    seen = {folder_id}
    current = folder_id
    while True:
        row = conn.execute(
            "SELECT parent_id FROM folders WHERE id = ?", (current,)
        ).fetchone()
        if row is None or row["parent_id"] is None:
            return depth
        current = int(row["parent_id"])
        if current in seen:
            # A cycle cannot be created through this module, but a database
            # edited by hand should not hang the server.
            raise OrganiseError("Those folders contain each other.")
        seen.add(current)
        depth += 1


def create_folder(conn: sqlite3.Connection, payload: dict) -> dict[str, object]:
    name = _name(payload, "folder")
    parent_id = _parent(conn, payload.get("parent_id"))

    cursor = conn.execute(
        "INSERT INTO folders (name, parent_id, visible) VALUES (?, ?, 1)",
        (name, parent_id),
    )
    row = conn.execute(
        "SELECT * FROM folders WHERE id = ?", (cursor.lastrowid,)
    ).fetchone()
    return folder_as_dict(row)


def update_folder(
    conn: sqlite3.Connection, folder_id: int, payload: dict
) -> dict[str, object]:
    existing = conn.execute(
        "SELECT * FROM folders WHERE id = ?", (folder_id,)
    ).fetchone()
    if existing is None:
        raise KeyError(folder_id)

    name = _name({**folder_as_dict(existing), **payload}, "folder")

    if "parent_id" in payload:
        parent_id = _parent(conn, payload.get("parent_id"))
        if parent_id == folder_id:
            raise OrganiseError("A folder cannot be inside itself.")
    else:
        parent_id = existing["parent_id"]

    visible = existing["visible"]
    if "visible" in payload:
        visible = 1 if payload["visible"] in (True, 1, "true", "1") else 0

    conn.execute(
        "UPDATE folders SET name = ?, parent_id = ?, visible = ? WHERE id = ?",
        (name, parent_id, visible, folder_id),
    )
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    return folder_as_dict(row)


def delete_folder(conn: sqlite3.Connection, folder_id: int) -> dict[str, object]:
    """Remove a folder and everything filed under it - the folders, not the pins.

    A folder is a place to put things, so deleting one is a filing decision
    rather than a decision about the places themselves. Its pins come back out
    as unfiled, which is where they would have been had the folder never
    existed.
    """
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        raise KeyError(folder_id)

    doomed = [folder_id]
    frontier = [folder_id]
    while frontier:
        children = conn.execute(
            "SELECT id FROM folders WHERE parent_id = ?", (frontier.pop(),)
        ).fetchall()
        for child in children:
            doomed.append(int(child["id"]))
            frontier.append(int(child["id"]))

    marks = ",".join("?" * len(doomed))
    conn.execute(f"UPDATE places SET folder_id = NULL WHERE folder_id IN ({marks})", doomed)
    conn.execute(f"DELETE FROM folders WHERE id IN ({marks})", doomed)
    return folder_as_dict(row)


