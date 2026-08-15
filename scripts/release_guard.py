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
  release_guard.py cut <version> [--date D] rename Unreleased to a release
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

UNRELEASED = "## [Unreleased]"

# Placeholder text that means "no entries here yet". It belongs under
# Unreleased and must never survive into a published release, so releasing
# with one still in the section is a hard failure rather than a wart on the
# release page.
PLACEHOLDERS = {"nothing yet.", "- nothing yet.", "tbd", "todo"}


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

    stray = [line for line in body if line.strip().lower() in PLACEHOLDERS]
    if stray:
        raise SystemExit(
            f"CHANGELOG.md's section for {version} still contains a "
            f"placeholder line ({stray[0].strip()!r}). That belongs under "
            "Unreleased, not on a release page. Remove it before tagging."
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


def cut(version: str, date: str, summary: str = "") -> None:
    """Turn the Unreleased section into a dated release section.

    Doing this by hand is how a placeholder line ends up on a release page:
    inserting a heading above Unreleased leaves whatever was under it sitting
    beneath the new version instead. This moves the entries and leaves a clean
    Unreleased behind.
    """
    changelog = ROOT / "CHANGELOG.md"
    lines = changelog.read_text(encoding="utf-8").splitlines()

    try:
        start = next(i for i, line in enumerate(lines) if line.startswith(UNRELEASED))
    except StopIteration:
        raise SystemExit("CHANGELOG.md has no '## [Unreleased]' section to cut.") from None

    end = start + 1
    while end < len(lines) and not lines[end].startswith("## "):
        end += 1

    entries = [
        line
        for line in lines[start + 1 : end]
        if line.strip().lower() not in PLACEHOLDERS
    ]
    while entries and not entries[0].strip():
        entries.pop(0)
    while entries and not entries[-1].strip():
        entries.pop()

    if not entries:
        raise SystemExit(
            f"Nothing to release. CHANGELOG.md's Unreleased section has no "
            f"entries, so {version} would ship with blank release notes."
        )

    body = [UNRELEASED, "", "Nothing yet.", "", f"## [{version}] - {date}", ""]
    if summary:
        body += [summary, ""]
    body += entries + [""]

    lines[start:end] = body
    changelog.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"cut {version} with {len([e for e in entries if e.startswith('- ')])} entries")


def main() -> int:
    parser = argparse.ArgumentParser(description="Irfaran release checks")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("check", "section"):
        one = sub.add_parser(name)
        one.add_argument("version")

    cutter = sub.add_parser("cut")
    cutter.add_argument("version")
    cutter.add_argument("--date", required=True)
    cutter.add_argument("--summary", default="")

    args = parser.parse_args()
    if args.command == "check":
        check(args.version)
    elif args.command == "cut":
        cut(args.version, args.date, args.summary)
    else:
        print(changelog_section(args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
