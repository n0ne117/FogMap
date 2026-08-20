# SPDX-License-Identifier: AGPL-3.0-or-later
"""A progress bar has to be put away by whoever painted it.

Reported after a run of reveal strokes: the fog was cleared, every point was
drawn, and the bar above the time bar stayed at about three quarters for good.

The mechanism is in `notice()`. `show()` sets a timer that hides a good-news
message after four seconds; `progress()` cancels that timer, because a bar that
vanishes mid-render is worse than one that sits there. So a caller that paints
progress owns putting the notice back into a state that ends - and the watcher
callback deliberately ignores the final poll, since there is no progress to
report once a render is finished. Nothing was left to clear the bar.

Source-level rather than behavioural, for the same reason test_markup.py is:
there is no test runner for the TypeScript, and a guard that reads the source is
worth more than no guard at all for a failure that is invisible until somebody
draws for a while and then waits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from .test_markup import web_dir

WEB = web_dir()


def source(name: str) -> str:
    path = WEB / "src" / name
    assert path.is_file(), f"{name} is missing, so this test checks nothing"
    return path.read_text()


def body_of(text: str, signature: str) -> str:
    """The text of one method, from its signature to the matching brace."""
    start = text.index(signature)
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"no closing brace for {signature}")


class TestDrawing:
    def test_following_a_render_ends_by_setting_a_message(self) -> None:
        """The message is what re-arms the timer that hides the notice."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "onStatus(" in body, (
            "followTheRender paints progress but never puts the notice back, so "
            "the bar is left wherever the last poll found it"
        )

    def test_it_is_given_something_to_say(self) -> None:
        """A summary rather than an empty string, or the notice just disappears."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "summary" in body

    def test_both_paths_hand_it_a_summary(self) -> None:
        """Drawing a stroke and undoing one both paint progress."""
        text = source("draw.ts")
        calls = re.findall(r"this\.followTheRender\(([^)]*)\)", text)
        assert len(calls) == 2, f"expected drawing and undo, found {calls}"
        assert all(argument.strip() for argument in calls), (
            f"followTheRender called with nothing to say afterwards: {calls}"
        )

    def test_losing_track_is_said_out_loud(self) -> None:
        """Silence would leave the same stuck bar by another route."""
        body = body_of(source("draw.ts"), "private async followTheRender(")
        assert "null" in body and "In progress" in body


class TestTheNoticeItself:
    def test_progress_cancels_the_hide_timer(self) -> None:
        """The reason a caller has to put it back. If this changes, so does that."""
        # The implementation, not the interface declaration above it - the
        # default argument is what tells them apart.
        body = body_of(source("ui.ts"), "progress(done: number, total: number, label = ")
        assert "clearTimer()" in body

    def test_show_arms_the_hide_timer(self) -> None:
        body = body_of(source("ui.ts"), "show(message: string, bad = false)")
        assert "setTimeout(hide" in body


class TestEverywhereElseThatPaintsProgress:
    @pytest.mark.parametrize("name", ["imports.ts", "progress.ts"])
    def test_it_also_settles_on_something(self, name: str) -> None:
        """Whoever calls progress() must have a terminal branch as well."""
        text = source(name)
        if ".progress(" not in text and "progress-bar" not in text:
            pytest.skip(f"{name} paints no progress bar")
        assert "textContent" in text or "show(" in text, (
            f"{name} paints progress with nothing that ends it"
        )


class TestWiringIsFaultIsolated:
    """One panel failing must not take the rest of the page with it.

    Reported as "a token is set, but the Import button does not react". The
    wiring in start() ran in a straight line, so the first component to throw
    took every handler after it - and the Import button is wired forty lines
    below the search bar. Nothing appeared on screen; the console was the only
    clue, and only if you thought to look.

    Verified by breaking one id and reloading: the banner said "search", and the
    Import button still worked.
    """

    def main_source(self) -> str:
        return source("main.ts")

    def test_every_component_is_wired_through_the_guard(self) -> None:
        text = self.main_source()
        bare = re.findall(r"\n  ([A-Za-z_]\w*)\.wire\(\)", text)
        assert not bare, (
            f"wired without the guard, so a throw here kills every handler "
            f"after it: {', '.join(sorted(set(bare)))}. Use wirePart('name', "
            "() => x.wire())."
        )

    def test_the_guard_exists_and_reports(self) -> None:
        body = body_of(self.main_source(), "function wirePart(")
        assert "catch" in body, "wirePart does not survive a failure"
        assert "console.error" in body, "a silent failure is the original bug"
        assert "wiring-error" in body, "nothing tells the person at the screen"

    def test_something_is_actually_wired_through_it(self) -> None:
        """A guard nothing uses would make the test above vacuous."""
        assert self.main_source().count("wirePart(") > 5
