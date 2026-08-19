# SPDX-License-Identifier: AGPL-3.0-or-later
"""Test setup.

The data directory is redirected to a temporary path before anything imports
`irfaran.db`, so a test run can never touch a real database.
"""

from __future__ import annotations

import os
import tempfile

os.environ["IRFARAN_DATA_DIR"] = tempfile.mkdtemp(prefix="irfaran-test-")
os.environ.pop("IRFARAN_TOKEN", None)


import pytest


@pytest.fixture(autouse=True)
def quiet_queue():
    """Stop any render a previous test left running, before this one starts.

    The render queue is one object for the whole process and keeps taking passes
    while work is owed, so a test that defers work can leave a worker writing
    tiles - and rows - into the next test's setup. That surfaces a long way from
    its cause: what it looked like was a live-ingest request being told the
    server was busy, in a file that passes perfectly well on its own.

    Imported inside the fixture because the data directory above has to be set
    before anything pulls in irfaran.db.
    """
    import time

    from irfaran import renderq

    deadline = time.monotonic() + 120
    while renderq.queue.running and time.monotonic() < deadline:
        time.sleep(0.02)

    # Waited out rather than stopped. Stopping abandons the debt, and the next
    # test then opens with tiles already owing a render - which is how a test
    # asserting "nothing is pending" came to fail for reasons that had nothing
    # to do with it. Whatever is left over is cleared here instead: both tables
    # are derived, transient, and nobody's fixture should inherit them.
    import sqlite3

    from irfaran import db

    conn = db.connect()
    try:
        with db.transaction(conn):
            db.clear_pending_render(conn)
            db.clear_render_done(conn)
    except sqlite3.OperationalError:
        # No schema yet, so this is the first test to touch the database and
        # there is nothing left over by definition. Creating it here instead
        # would hand every test a database it did not ask for.
        pass
    finally:
        conn.close()
    yield
