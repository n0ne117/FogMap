# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading configuration from the environment.

Everything here exists for one reason: the project was called FogMap until
0.10.0, and somebody's .env still says so. A rename that silently ignores the
token an install has been running on would lock them out of their own map, so
the old prefix keeps working and the new one wins where both are set.
"""

from __future__ import annotations

import os

PREFIX = "IRFARAN_"
LEGACY_PREFIX = "FOGMAP_"


def get(name: str, default: str = "") -> str:
    """Read IRFARAN_<name>, falling back to FOGMAP_<name>."""
    value = os.environ.get(PREFIX + name, "").strip()
    if value:
        return value
    return os.environ.get(LEGACY_PREFIX + name, "").strip() or default


def names(name: str) -> tuple[str, str]:
    """Both spellings of a variable, for error messages that name them."""
    return PREFIX + name, LEGACY_PREFIX + name
