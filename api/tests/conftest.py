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
