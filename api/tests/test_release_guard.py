# SPDX-License-Identifier: AGPL-3.0-or-later
"""The release guard.

CI refuses to publish when the tag, the VERSION file and the changelog
disagree. These tests exist because that check is the only thing standing
between a mistyped tag and an image whose /healthz reports a version it is
not.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

def find_guard() -> Path:
    """Locate the script whether we are in a checkout or in the test image.

    The repository nests it under api/../scripts; the image flattens
    everything under /app. Searching upwards covers both.
    """
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "scripts" / "release_guard.py"
        if candidate.is_file():
            return candidate
    raise AssertionError(
        "scripts/release_guard.py was not found above "
        f"{Path(__file__).resolve()}. The test image did not copy it in."
    )


GUARD = find_guard()
REPO_ROOT = GUARD.parent.parent


def load_guard(root: Path):
    """Import release_guard with its ROOT pointed at a throwaway repo."""
    spec = importlib.util.spec_from_file_location("release_guard", GUARD)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.ROOT = root
    return module


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n"
        "\n"
        "## [Unreleased]\n"
        "\n"
        "### Added\n"
        "- Nothing yet.\n"
        "\n"
        "## [0.2.0] - 2026-08-14\n"
        "\n"
        "### Added\n"
        "- A tile server and a map to look at it with.\n"
        "\n"
        "## [0.1.0] - 2026-08-01\n"
        "\n"
        "### Added\n"
        "- Ingest and raster core.\n"
        "\n"
        "## [0.0.9] - 2026-07-01\n"
        "\n"
        "[Unreleased]: https://example.org/compare\n"
        "[0.2.0]: https://example.org/tag/v0.2.0\n",
        encoding="utf-8",
    )
    return tmp_path


class TestVersionMatch:
    def test_a_matching_tag_passes(self, repo, capsys):
        load_guard(repo).check("0.2.0")
        assert "matches VERSION" in capsys.readouterr().out

    def test_a_mismatched_tag_fails_the_build(self, repo):
        with pytest.raises(SystemExit, match="VERSION says 0.2.0 but the tag"):
            load_guard(repo).check("0.3.0")

    def test_the_message_says_how_to_fix_it(self, repo):
        with pytest.raises(SystemExit, match="Fix VERSION, commit, and move the tag"):
            load_guard(repo).check("1.0.0")


class TestChangelogSection:
    def test_the_right_section_is_extracted(self, repo):
        section = load_guard(repo).changelog_section("0.2.0")
        assert "A tile server and a map to look at it with." in section
        assert "Ingest and raster core." not in section

    def test_the_heading_itself_is_not_included(self, repo):
        assert "## [0.2.0]" not in load_guard(repo).changelog_section("0.2.0")

    def test_link_footnotes_are_stripped(self, repo):
        assert "https://example.org" not in load_guard(repo).changelog_section("0.2.0")

    def test_a_missing_section_fails_the_build(self, repo):
        with pytest.raises(SystemExit, match="CHANGELOG.md has no section for 0.5.0"):
            load_guard(repo).changelog_section("0.5.0")

    def test_a_missing_section_is_never_downgraded_to_a_warning(self, repo):
        # The build plan is explicit that this is the enforcement mechanism.
        with pytest.raises(SystemExit, match="Refusing to publish"):
            load_guard(repo).changelog_section("0.5.0")

    def test_an_empty_section_is_refused_too(self, repo):
        with pytest.raises(SystemExit, match="but it is empty"):
            load_guard(repo).changelog_section("0.0.9")

    def test_the_last_section_in_the_file_still_reads(self, repo):
        assert "Ingest and raster core." in load_guard(repo).changelog_section("0.1.0")


class TestAgainstTheRealRepository:
    def test_the_shipped_changelog_has_notes_for_the_released_version(self):
        section = load_guard(REPO_ROOT).changelog_section("0.1.0")
        assert section.strip()
