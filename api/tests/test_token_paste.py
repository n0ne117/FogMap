# SPDX-License-Identifier: AGPL-3.0-or-later
"""Getting a token out of the server and back into a browser.

Both halves of one bug. `irfaran.cli token` printed the token and then a line
saying where it came from, both on stdout - and the obvious thing to do with two
lines of console output is select both and paste them. That was rejected as the
wrong token, with the right token sitting in the clipboard one line up.

Two fixes, because the first one was not enough. The token now goes on stdout
alone, with its provenance on stderr, so a pipe or a $(...) gets the token and
nothing else. And the fields that receive it take the first *line* of a paste -
not the first whitespace-delimited chunk, which was tried first and cannot
work: a text input strips CR and LF, so by the time anything reads .value the
two lines have been welded together with no whitespace left to split on.
"""

from __future__ import annotations

import io
import re
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from irfaran import cli, db, tokens

HEX_TOKEN = re.compile(r"^[0-9a-f]{48}$")


def run_token_command() -> tuple[str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        assert cli.main(["token"]) == 0
    return out.getvalue(), err.getvalue()


class TestTheCommand:
    def test_stdout_is_the_token_and_nothing_else(self) -> None:
        """So $(...) and a pipe get the token, not the token plus prose."""
        out, _ = run_token_command()
        assert len(out.strip().splitlines()) == 1
        conn = db.connect()
        try:
            expected, _ = tokens.resolve(conn)
        finally:
            conn.close()
        assert out.strip() == expected

    def test_where_it_came_from_goes_to_stderr(self) -> None:
        _, err = run_token_command()
        assert err.strip()
        assert "environment" in err or "generated" in err

    def test_the_provenance_is_not_on_stdout(self) -> None:
        out, _ = run_token_command()
        assert "environment" not in out
        assert "generated" not in out

    def test_the_token_is_never_split_across_lines(self) -> None:
        out, _ = run_token_command()
        assert " " not in out.strip()


class TestPastingItBack:
    """The browser side.

    There is no JavaScript test runner here, so what is asserted is that the
    shipped source still uses the mechanism that works - which matters more
    than usual, because the obvious mechanism does not work and was tried
    first.

    A text input runs a value sanitisation algorithm that strips CR and LF. So
    a two-line paste is not "token, newline, prose" by the time anything reads
    .value - it is the token welded straight onto the prose with no whitespace
    between them, and no amount of trimming or splitting recovers it. The paste
    event still holds the original text, which is the only place the first line
    can be taken.
    """

    @staticmethod
    def source(name: str) -> str:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "web" / "src" / name
            if candidate.is_file():
                return candidate.read_text()
        raise AssertionError(f"web/src/{name} not found above this test")

    def test_the_paste_handler_exists(self) -> None:
        source = self.source("ui.ts")
        assert "export function acceptTokenPaste" in source
        assert "clipboardData" in source, (
            "acceptTokenPaste no longer reads the clipboard directly, which is "
            "the only place the newline still exists"
        )
        assert "preventDefault" in source, (
            "without preventDefault the browser inserts the sanitised value "
            "over the corrected one"
        )

    def test_it_splits_on_line_endings_not_on_whitespace(self) -> None:
        """The distinction is the whole bug: whitespace splitting cannot work."""
        source = self.source("ui.ts")
        handler = source[source.index("export function acceptTokenPaste"):]
        handler = handler[: handler.index("\n}")]
        assert "\\r?\\n" in handler, "the split is not on line endings"

    def test_a_single_line_paste_is_left_alone(self) -> None:
        """It returns early, so the browser does its own insertion."""
        source = self.source("ui.ts")
        handler = source[source.index("export function acceptTokenPaste"):]
        handler = handler[: handler.index("\n}")]
        assert "return" in handler.split("preventDefault")[0], (
            "there is no early return before preventDefault, so ordinary "
            "pastes are being intercepted too"
        )

    def test_both_paste_targets_are_wired_up(self) -> None:
        """The setup screen and the Security tab, the two places people paste."""
        assert "acceptTokenPaste(input)" in self.source("ui.ts")
        assert "acceptTokenPaste(known)" in self.source("setup.ts")

    def test_trailing_whitespace_is_still_handled(self) -> None:
        """tokenFrom stays for the ordinary case of a trailing space."""
        source = self.source("api.ts")
        assert "export function tokenFrom" in source


class TestApplyingIt:
    """The Apply button on Settings, Security.

    Storing on the field's `input` event alone is not enough, and the failure is
    a nasty one: a password manager, an autofill or any extension sets .value
    directly without dispatching an input event, so the field visibly holds the
    right token, nothing is stored, the status line says "No token set", and
    every write is refused while the answer is on screen. Reproduced in a
    browser before this was added.
    """

    @staticmethod
    def source(name: str) -> str:
        for parent in Path(__file__).resolve().parents:
            candidate = parent / "web" / "src" / name
            if candidate.is_file():
                return candidate.read_text()
        raise AssertionError(f"web/src/{name} not found above this test")

    def test_the_button_is_in_the_markup(self) -> None:
        for parent in Path(__file__).resolve().parents:
            page = parent / "web" / "index.html"
            if page.is_file():
                assert 'id="token-apply"' in page.read_text()
                return
        raise AssertionError("web/index.html not found above this test")

    def test_it_reads_the_field_rather_than_trusting_an_event(self) -> None:
        source = self.source("ui.ts")
        handler = source[source.index("const apply = element"):]
        assert "input.value" in handler, (
            "Apply does not read the field, so a value put there by a password "
            "manager is still invisible to it"
        )

    def test_it_verifies_before_believing_the_token(self) -> None:
        source = self.source("ui.ts")
        handler = source[source.index("const apply = element"):]
        assert "apiSend" in handler, "Apply stores without checking with the server"

    def test_a_refused_token_restores_the_previous_one(self) -> None:
        """Otherwise one bad paste locks a working browser out."""
        source = self.source("ui.ts")
        handler = source[source.index("const apply = element"):]
        assert "setToken(previous)" in handler

    def test_blocked_storage_is_reported_not_blamed_on_the_token(self) -> None:
        """Private browsing swallows setToken silently, which looks identical."""
        source = self.source("ui.ts")
        handler = source[source.index("const apply = element"):]
        assert "getToken() !== candidate" in handler

