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


class TestPlaceholderGuard:
    """A placeholder must never reach a release page.

    This exists because one did: cutting 0.3.0 by hand left 'Nothing yet.'
    sitting inside the released section, and it was published before anyone
    noticed. Now it fails the build.
    """

    @pytest.fixture
    def with_placeholder(self, tmp_path):
        (tmp_path / "VERSION").write_text("0.4.0\n", encoding="utf-8")
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n"
            "\n"
            "## [Unreleased]\n"
            "\n"
            "Nothing yet.\n"
            "\n"
            "## [0.4.0] - 2026-09-01\n"
            "\n"
            "A summary line.\n"
            "\n"
            "Nothing yet.\n"
            "\n"
            "### Added\n"
            "- Something real.\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_a_stray_placeholder_fails_the_build(self, with_placeholder):
        with pytest.raises(SystemExit, match="still contains a placeholder line"):
            load_guard(with_placeholder).changelog_section("0.4.0")

    def test_the_message_says_where_it_belongs(self, with_placeholder):
        with pytest.raises(SystemExit, match="belongs under\nUnreleased|belongs under"):
            load_guard(with_placeholder).changelog_section("0.4.0")

    def test_check_refuses_the_release_too(self, with_placeholder):
        with pytest.raises(SystemExit, match="placeholder"):
            load_guard(with_placeholder).check("0.4.0")


class TestCut:
    @pytest.fixture
    def pending(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n"
            "\n"
            "## [Unreleased]\n"
            "\n"
            "Nothing yet.\n"
            "\n"
            "### Added\n"
            "- A year slider.\n"
            "\n"
            "### Fixed\n"
            "- A caching bug.\n"
            "\n"
            "## [0.2.0] - 2026-08-01\n"
            "\n"
            "### Added\n"
            "- Tiles.\n",
            encoding="utf-8",
        )
        return tmp_path

    def test_entries_move_into_the_new_version(self, pending):
        guard = load_guard(pending)
        guard.cut("0.3.0", "2026-08-14")

        section = guard.changelog_section("0.3.0")
        assert "A year slider." in section
        assert "A caching bug." in section
        assert "Tiles." not in section

    def test_the_placeholder_does_not_follow_the_entries(self, pending):
        guard = load_guard(pending)
        guard.cut("0.3.0", "2026-08-14")
        # The exact bug this command exists to prevent.
        assert "Nothing yet." not in guard.changelog_section("0.3.0")

    def test_a_fresh_unreleased_section_is_left_behind(self, pending):
        guard = load_guard(pending)
        guard.cut("0.3.0", "2026-08-14")

        text = (pending / "CHANGELOG.md").read_text(encoding="utf-8")
        unreleased = text.split("## [Unreleased]")[1].split("## [")[0]
        assert "Nothing yet." in unreleased
        assert "A year slider." not in unreleased

    def test_a_summary_is_placed_above_the_entries(self, pending):
        guard = load_guard(pending)
        guard.cut("0.3.0", "2026-08-14", "Time, at last.")

        section = guard.changelog_section("0.3.0")
        assert section.splitlines()[0] == "Time, at last."

    def test_the_older_release_is_left_untouched(self, pending):
        guard = load_guard(pending)
        guard.cut("0.3.0", "2026-08-14")
        assert "Tiles." in guard.changelog_section("0.2.0")

    def test_cutting_with_nothing_to_release_is_refused(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [Unreleased]\n\nNothing yet.\n\n## [0.1.0] - 2026-01-01\n"
            "\n### Added\n- Things.\n",
            encoding="utf-8",
        )
        with pytest.raises(SystemExit, match="Nothing to release"):
            load_guard(tmp_path).cut("0.2.0", "2026-08-14")


class TestAgainstTheRealRepository:
    def test_the_shipped_changelog_has_notes_for_the_released_version(self):
        section = load_guard(REPO_ROOT).changelog_section("0.1.0")
        assert section.strip()
