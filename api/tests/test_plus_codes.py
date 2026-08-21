# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plus Codes: coordinates written differently.

Open Location Code needs no data - `8FVC9G8F+6W` decodes with arithmetic, where a
postal code would need a table mapping codes to places. That is why it belongs
here at all: nothing is looked up and nothing leaves the machine.

The decoder is checked against three points that can be verified independently:
Zurich, Google's own documented example at 1600 Amphitheatre Parkway, and a
vector from the specification. If those three land, the pair-and-grid arithmetic
is right.

Short codes are the interesting half. They are missing their leading digits and
are recovered against a reference position, so they are only unambiguous within
about half a degree of it - and a wrong recovery cannot be detected, only shown.
With the map on Sydney, a Zurich short code resolves near Sydney, confidently and
silently. So the recovered full code is handed back with the answer.
"""

from __future__ import annotations

import pytest

from irfaran import db, pluscode, search

TOKEN = "plus-code-token"

#: Zurich. The full code, its short form, and where both should land.
FULL = "8FVC9G8F+6W"
SHORT = "9G8F+6W"
LAT, LON = 47.3655625, 8.5248125


@pytest.fixture
def conn(monkeypatch, tmp_path):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "plus"))
    connection = db.open_initialised()
    with db.transaction(connection):
        for key in ("search_plus_codes", "search_plus_codes_short"):
            connection.execute(
                "INSERT INTO settings (key, value) VALUES (?, 'true') "
                "ON CONFLICT(key) DO UPDATE SET value = 'true'",
                (key,),
            )
    yield connection
    connection.close()


class TestDecoding:
    @pytest.mark.parametrize(
        "code, lat, lon",
        [
            (FULL, LAT, LON),
            # Google's own example: 1600 Amphitheatre Parkway.
            ("849VCWC8+R9", 37.4220625, -122.0840625),
            # From the specification.
            ("8FVC2222+22", 47.0000625, 8.0000625),
        ],
    )
    def test_known_codes_land_where_they_should(self, code, lat, lon) -> None:
        got_lat, got_lon = pluscode.decode(code)
        assert got_lat == pytest.approx(lat, abs=1e-6)
        assert got_lon == pytest.approx(lon, abs=1e-6)

    def test_it_round_trips(self, code=FULL) -> None:
        """Encoding what a code decodes to has to give the code back."""
        assert pluscode.encode(*pluscode.decode(code)) == code

    def test_the_middle_of_the_cell_is_returned(self) -> None:
        """A code names an area; its middle is the honest point to stand for it."""
        lat, lon = pluscode.decode("8FVC2222+22")
        assert lat > 47.0 and lon > 8.0, "a corner was returned, not the centre"


class TestWhatIsNotAPlusCode:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "Vienna",
            "caorle",
            "27.74367, -15.58338",
            "8FVC9G8F",       # no separator
            "9G8F+6",         # too few after it
            "+6W",            # nothing before it: resolvable only from ~100 m
            "8FVC0000+",      # a padded area code, not a location
            "AEIOU9G8F+6W",   # letters that are not in the alphabet
            "8FVC9G8F+6W+2",  # two separators
        ],
    )
    def test_it_is_refused(self, text) -> None:
        assert not pluscode.is_full(text)
        assert not pluscode.is_short(text)

    def test_a_vowel_free_word_is_not_claimed(self) -> None:
        """The alphabet has no vowels, so a narrow test matters: no separator."""
        assert not pluscode.looks_like("HMPQ")


class TestShortCodes:
    def test_it_resolves_against_a_nearby_reference(self) -> None:
        lat, lon = pluscode.recover(SHORT, 47.37, 8.54)
        assert (lat, lon) == pytest.approx((LAT, LON), abs=1e-6)

    def test_it_still_resolves_fifty_kilometres_away(self) -> None:
        lat, lon = pluscode.recover(SHORT, 47.0, 9.0)
        assert (lat, lon) == pytest.approx((LAT, LON), abs=1e-6)

    def test_a_distant_reference_gives_a_different_place(self) -> None:
        """Not a bug - the property of short codes, and why they are opt-in."""
        lat, lon = pluscode.recover(SHORT, -33.9, 151.2)
        assert lat < 0, "a Zurich code resolved from Sydney should not be in Zurich"

    def test_the_recovered_code_shows_where_it_went(self) -> None:
        """The only way a wrong recovery can be noticed."""
        near = pluscode.encode(*pluscode.recover(SHORT, 47.37, 8.54))
        far = pluscode.encode(*pluscode.recover(SHORT, -33.9, 151.2))
        assert near == FULL
        assert far != FULL and far.endswith("9G8F+6W")


class TestThroughSearch:
    def test_a_full_code_is_a_result(self, conn) -> None:
        answer = search.search(conn, FULL)
        assert [hit["kind"] for hit in answer["results"]] == ["pluscode"]
        assert answer["results"][0]["lat"] == pytest.approx(LAT, abs=1e-6)

    def test_a_short_code_uses_the_reference(self, conn) -> None:
        answer = search.search(conn, SHORT, (47.37, 8.54))
        assert answer["results"][0]["lat"] == pytest.approx(LAT, abs=1e-6)
        assert answer["results"][0]["label"] == FULL

    def test_a_short_code_says_it_was_resolved(self, conn) -> None:
        answer = search.search(conn, SHORT, (47.37, 8.54))
        assert "resolved" in str(answer["results"][0]["detail"])

    def test_a_short_code_without_a_reference_explains_itself(self, conn) -> None:
        answer = search.search(conn, SHORT)
        assert answer["results"] == []
        assert "resolved from somewhere" in answer["hint"]

    def test_a_full_code_needs_no_reference(self, conn) -> None:
        assert search.search(conn, FULL)["results"]


class TestTheToggles:
    def test_it_is_off_on_a_fresh_database(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "fresh"))
        fresh = db.open_initialised()
        try:
            assert search.included(fresh)["plus_codes"] is False
        finally:
            fresh.close()

    def test_a_full_code_says_why_it_is_ignored(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "off"))
        fresh = db.open_initialised()
        try:
            answer = search.search(fresh, FULL)
            assert answer["results"] == []
            assert "Plus Codes are switched off" in answer["hint"]
        finally:
            fresh.close()

    def test_one_switch_covers_both_forms(self, conn) -> None:
        """It used to be two.

        A short code is resolved from wherever the map is looking, so it can be
        confidently wrong, and that argued for its own switch. But "use where I
        am looking" is what the search bar's own toggle says, and saying it in
        two places is one too many - the recovered full code in the answer is
        what makes a wrong resolution visible. Alex's call, and the simpler one.
        """
        assert search.search(conn, FULL)["results"]
        assert search.search(conn, SHORT, (47.37, 8.54))["results"]

    def test_switching_it_off_stops_both(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "off"))
        fresh = db.open_initialised()
        try:
            assert search.search(fresh, FULL)["results"] == []
            assert search.search(fresh, SHORT, (47.37, 8.54))["results"] == []
        finally:
            fresh.close()

    def test_a_pin_search_is_not_told_about_plus_codes(self, conn) -> None:
        """Noise about a thing nobody asked for."""
        answer = search.search(conn, "definitely-not-here")
        assert "Plus Code" not in answer["hint"]

    def test_it_travels_with_an_export(self) -> None:
        from irfaran import transfer

        assert "search_plus_codes" in transfer.PORTABLE_SETTINGS
