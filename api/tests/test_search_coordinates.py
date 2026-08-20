# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a coordinate somebody pasted.

The use case is paste, not typing, so the input is whatever the place they
copied it from emitted: a comma or a space, a degree sign or none, a hemisphere
letter before or after, degrees and minutes and seconds. All of it has to land
on the same point, and anything that is not a coordinate has to be refused
rather than approximated - being taken confidently to the wrong continent is
worse than being told the input was not understood.
"""

from __future__ import annotations

import pytest

from irfaran import search

#: Gran Canaria, which is where the example in the request points.
LAT, LON = 27.74367, -15.58338


class TestDecimalDegrees:
    @pytest.mark.parametrize(
        "text",
        [
            "27.74367, -15.58338",
            "27.74367,-15.58338",
            "27.74367 -15.58338",
            "  27.74367 ,  -15.58338  ",
            "+27.74367, -15.58338",
            "27.74367°, -15.58338°",
        ],
    )
    def test_the_same_point_however_it_was_written(self, text) -> None:
        assert search.parse_coordinates(text) == pytest.approx((LAT, LON))

    @pytest.mark.parametrize(
        "text",
        [
            "27.74367N, 15.58338W",
            "27.74367 N 15.58338 W",
            "N27.74367, W15.58338",
            "27.74367° N, 15.58338° W",
        ],
    )
    def test_hemisphere_letters_stand_in_for_the_sign(self, text) -> None:
        assert search.parse_coordinates(text) == pytest.approx((LAT, LON))

    def test_a_letter_agreeing_with_the_sign_is_accepted(self) -> None:
        """-15.58338W says west twice and means it once."""
        assert search.parse_coordinates("27.74367, -15.58338W") == pytest.approx((LAT, LON))

    def test_a_letter_contradicting_the_sign_is_refused(self) -> None:
        """-15.58338E cannot be resolved without guessing which half was meant."""
        assert search.parse_coordinates("27.74367, -15.58338E") is None

    def test_a_letter_on_both_sides_is_refused(self) -> None:
        assert search.parse_coordinates("N27.74367N, W15.58338W") is None

    def test_zero_is_a_place(self) -> None:
        """Null Island is where every test fixture lives. It has to parse."""
        assert search.parse_coordinates("0, 0") == (0.0, 0.0)

    def test_the_limits_themselves_are_inside(self) -> None:
        assert search.parse_coordinates("90, 180") == (90.0, 180.0)
        assert search.parse_coordinates("-90, -180") == (-90.0, -180.0)


class TestDegreesMinutesSeconds:
    def test_the_form_a_mapping_site_copies(self) -> None:
        lat, lon = search.parse_coordinates("27°44'37.2\"N 15°35'00.2\"W")
        assert lat == pytest.approx(27.7437, abs=1e-4)
        assert lon == pytest.approx(-15.5834, abs=1e-4)

    def test_typographic_quote_marks(self) -> None:
        """What a word processor or a website turns those marks into."""
        lat, lon = search.parse_coordinates("27°44′37.2″N 15°35′00.2″W")
        assert lat == pytest.approx(27.7437, abs=1e-4)
        assert lon == pytest.approx(-15.5834, abs=1e-4)

    def test_degrees_and_minutes_without_seconds(self) -> None:
        lat, lon = search.parse_coordinates("27°44'N 15°35'W")
        assert lat == pytest.approx(27.7333, abs=1e-4)
        assert lon == pytest.approx(-15.5833, abs=1e-4)

    def test_minutes_of_sixty_are_refused(self) -> None:
        """A real coordinate never has them, so this is a typo, not a place."""
        assert search.parse_coordinates("27°60'00\"N 15°35'00\"W") is None

    def test_seconds_of_sixty_are_refused(self) -> None:
        assert search.parse_coordinates("27°44'60\"N 15°35'00\"W") is None


class TestWhatIsNotACoordinate:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "Vienna",
            "27.74367",
            "hello, world",
            "1, 2, 3",
            "27.74367, -15.58338, 15z",
            "https://example.com/maps/@27.74367,-15.58338,15z",
        ],
    )
    def test_it_is_refused_rather_than_guessed_at(self, text) -> None:
        assert search.parse_coordinates(text) is None

    def test_out_of_range_in_both_directions_is_refused(self) -> None:
        assert search.parse_coordinates("500, 900") is None

    def test_latitude_beyond_the_pole_is_named_as_reversible(self) -> None:
        """91 is not a latitude, but 10, 91 is a place - so say that.

        Written as an assertion that this returns None first, which was wrong:
        refusing outright throws away the one useful thing that can be said
        about it.
        """
        with pytest.raises(search.Ambiguous):
            search.parse_coordinates("91, 10")


class TestTheOrderTheyWereWritten:
    def test_longitude_first_is_recognised_and_named(self) -> None:
        """A real mistake with a clear signature, worth saying out loud.

        The example first written here was "-15.58338, 27.74367", which is not
        ambiguous at all: both halves are in range as written, so it is a place
        off the coast of Africa and taking it as anything else would be the
        invention this exists to avoid. The signature of a reversed pair is a
        first number that cannot be a latitude.
        """
        with pytest.raises(search.Ambiguous) as raised:
            search.parse_coordinates("120.5, 45.2")
        assert "other way round" in str(raised.value)

    def test_it_is_not_silently_swapped(self) -> None:
        """The whole point: a confident jump to the wrong place is the worst answer."""
        with pytest.raises(search.Ambiguous):
            search.parse_coordinates("-160.4, 80.1")

    def test_a_pair_that_works_both_ways_is_taken_as_written(self) -> None:
        """45, 20 is a place either way. Latitude first is the convention."""
        assert search.parse_coordinates("45, 20") == (45.0, 20.0)


class TestTheEndpoint:
    """GET /api/search, which is the shape pins and tracks will arrive in."""

    @pytest.fixture
    def client(self, monkeypatch, tmp_path):
        from fastapi.testclient import TestClient

        from irfaran.main import app

        monkeypatch.setenv("IRFARAN_TOKEN", "search-token")
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path / "search"))
        with TestClient(app) as test_client:
            yield test_client

    def test_a_coordinate_comes_back_as_a_result(self, client) -> None:
        body = client.get("/api/search", params={"q": "27.74367, -15.58338"}).json()
        assert len(body["results"]) == 1

        found = body["results"][0]
        assert found["kind"] == "coordinates"
        assert found["lat"] == pytest.approx(LAT)
        assert found["lon"] == pytest.approx(LON)
        assert found["label"]

    def test_it_needs_no_token(self, client) -> None:
        """A search box that demands credentials first is a search box nobody uses."""
        assert client.get("/api/search", params={"q": "0, 0"}).status_code == 200

    def test_nonsense_answers_with_a_hint_rather_than_an_error(self, client) -> None:
        """A word that is neither a coordinate nor anything in the archive.

        The hint used to say searching pins and tracks was not built yet. It is
        now, so it says what is searched instead - a stale hint is a small lie
        that costs somebody a real attempt.
        """
        body = client.get("/api/search", params={"q": "Vienna"}).json()
        assert body["results"] == []
        assert "name" in body["hint"] and "year" in body["hint"]

    def test_a_reversed_pair_says_so(self, client) -> None:
        body = client.get("/api/search", params={"q": "120.5, 45.2"}).json()
        assert body["results"] == []
        assert "other way round" in body["hint"]

    def test_an_empty_query_is_not_an_error(self, client) -> None:
        body = client.get("/api/search", params={"q": ""}).json()
        assert body["results"] == []
        assert body["hint"] == ""

    def test_no_query_at_all_is_not_an_error(self, client) -> None:
        assert client.get("/api/search").status_code == 200
