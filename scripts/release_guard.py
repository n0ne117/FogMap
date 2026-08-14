#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release consistency checks, run by CI before anything is published.

Two things must agree with the git tag being built, and if either disagrees
the build fails rather than warns. A published image whose version does not
match its tag is worse than no release at all, because the version reported
by /healthz becomes a lie.

Usage
  release_guard.py check <version>          verify VERSION and CHANGELOG
  release_guard.py section <version>        print that version's release notes
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def repo_version() -> str:
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def changelog_section(version: str) -> str:
    """The body of the `## [version]` block, without its heading.

    Raises if there is no such section. That is the enforcement mechanism the
    build plan asks for, so it must never degrade to a warning.
    """
    changelog = ROOT / "CHANGELOG.md"
    try:
        lines = changelog.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SystemExit(f"Cannot read {changelog} ({exc}).") from exc

    heading = f"## [{version}]"
    start = None
    for index, line in enumerate(lines):
        if line.startswith(heading):
            start = index + 1
            break

    if start is None:
        raise SystemExit(
            f"CHANGELOG.md has no section for {version}. Every version gets "
            f"release notes, so add a '## [{version}] - YYYY-MM-DD' section "
            "before tagging. Refusing to publish a release with no notes."
        )

    end = start
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1

    # Drop the link-reference footnotes and any blank padding.
    body = [line for line in lines[start:end] if not line.startswith("[")]
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()

    if not body:
        raise SystemExit(
            f"CHANGELOG.md has a section for {version} but it is empty. "
            "Release notes are written for a reader, not left blank."
        )
    return "\n".join(body)


def check(version: str) -> None:
    found = repo_version()
    if found != version:
        raise SystemExit(
            f"VERSION says {found} but the tag being built is v{version}. "
            "The tag, the VERSION file and the image tag must always agree. "
            "Fix VERSION, commit, and move the tag."
        )
    changelog_section(version)
    print(f"version {version} matches VERSION and has release notes")


def main() -> int:
    parser = argparse.ArgumentParser(description="FogMap release checks")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("check", "section"):
        one = sub.add_parser(name)
        one.add_argument("version")

    args = parser.parse_args()
    if args.command == "check":
        check(args.version)
    else:
        print(changelog_section(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
