# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a tile's blobs are found, which was the cost of every render.

Compositing asks for one tile at a time - kind, x, y - and the blobs table's
primary key is (kind, source, layer, x, y). Only `kind` was a usable prefix, so
everything after it was a scan, and because the table is WITHOUT ROWID the rows
being scanned carry their blobs with them: reading one tile's fog walked every
fog blob in the archive.

The pyramid walk does that for every native tile in a view, so the cost was the
tile count multiplied by the table size - quadratic in the archive. Measured on a
real 2,954-tile view, one whole-view walk went from 247.5 s to 8.8 s with the
index below, and that walk was the whole reason a hand-drawn stroke took five
minutes to appear.

These are plan tests rather than timing tests. A stopwatch in CI measures the CI
machine; the query plan is the thing that was actually wrong, and it either says
the index or it says a scan.
"""

from __future__ import annotations

import sqlite3

import pytest

from irfaran import db

LOOKUP = (
    "SELECT source, layer, data FROM blobs WHERE kind = ? AND x = ? AND y = ?"
)


@pytest.fixture
def conn():
    connection = db.open_initialised()
    yield connection
    connection.close()


def plan(connection, sql, params) -> str:
    rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", params).fetchall()
    return " | ".join(str(row["detail"]) for row in rows)


class TestTheIndexExists:
    def test_a_fresh_database_has_it(self, conn) -> None:
        names = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_blobs_tile" in names

    def test_a_database_that_predates_it_gains_it(self, tmp_path) -> None:
        """Upgrades matter more than fresh installs: every existing archive is one."""
        path = tmp_path / "old.db"
        old = db.open_initialised(path)
        old.execute("DROP INDEX idx_blobs_tile")
        assert "idx_blobs_tile" not in _indexes(old)
        old.close()

        # What a restart does.
        upgraded = db.open_initialised(path)
        try:
            assert "idx_blobs_tile" in _indexes(upgraded)
        finally:
            upgraded.close()


class TestTheLookupUsesIt:
    def test_finding_a_tiles_blobs_is_a_seek(self, conn) -> None:
        detail = plan(conn, LOOKUP, ("fog", 8214, 8180))
        assert "idx_blobs_tile" in detail, (
            f"the blob lookup is not using the index: {detail}"
        )

    def test_it_is_not_scanning_a_whole_kind(self, conn) -> None:
        """The exact plan that made a stroke cost minutes.

        `SEARCH blobs USING PRIMARY KEY (kind=?)` reads every row of that kind,
        blobs included. Seeing it again means the index has been lost or the
        query has been rewritten past it.
        """
        detail = plan(conn, LOOKUP, ("fog", 8214, 8180))
        assert "PRIMARY KEY (kind=?)" not in detail, (
            f"back to scanning every blob of a kind: {detail}"
        )

    def test_the_layer_filtered_form_uses_it_too(self, conn) -> None:
        """Compositing a year view adds a layer filter, and must not lose the seek."""
        sql = LOOKUP + " AND layer IN (?, ?) ORDER BY source, layer"
        detail = plan(conn, sql, ("fog", 8214, 8180, "2024", "2025"))
        assert "idx_blobs_tile" in detail, detail


def _indexes(connection: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        )
    }
