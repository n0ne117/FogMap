# SPDX-License-Identifier: AGPL-3.0-or-later
"""FogMap - a self-hosted fog-of-war location map."""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["__version__", "read_version"]

# Where VERSION may live, in order of preference. The Docker image copies it
# next to the package; a source checkout has it at the repository root.
_VERSION_CANDIDATES = (
    Path(__file__).resolve().parent.parent / "VERSION",
    Path(__file__).resolve().parents[2] / "VERSION",
    Path("/app/VERSION"),
)


def read_version() -> str:
    """Return the contents of the VERSION file, the sole source of truth.

    Raises rather than guessing. A build that cannot state its own version is
    broken, and finding that out at startup is far cheaper than finding it out
    while comparing a browser against a repository.
    """
    override = os.environ.get("FOGMAP_VERSION", "").strip()
    if override:
        return override

    for candidate in _VERSION_CANDIDATES:
        try:
            text = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if text:
            return text

    searched = ", ".join(str(path) for path in _VERSION_CANDIDATES)
    raise RuntimeError(
        "FogMap cannot determine its version. No readable VERSION file was "
        f"found at any of {searched}, and FOGMAP_VERSION is unset. The image "
        "was built without copying VERSION into it."
    )


__version__ = read_version()
