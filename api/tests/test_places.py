# SPDX-License-Identifier: AGPL-3.0-or-later
"""Named places: CRUD, the fog they clear, and filtering by person."""

from __future__ import annotations

import io

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from irfaran import db, geo, places
from irfaran.main import app

TOKEN = "synthetic-places-token"

# Open water near Null Island. Every name below is invented.
LAT = 0.42
LON = 0.71


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
    conn = db.open_initialised()
    conn.execute("DELETE FROM blobs")
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM places")
    conn.close()

    with TestClient(app) as test_client:
        yield test_client


def auth() -> dict:
    return {"X-Irfaran-Token": TOKEN}


def explored(client, view: str = "all", lat: float = LAT, lon: float = LON) -> int:
    tile_x, tile_y = geo.lonlat_to_tile(lon, lat)
    response = client.get(f"/api/tiles/dark/{view}/fog/14/{tile_x}/{tile_y}.png")
    pixels = np.array(Image.open(io.BytesIO(response.content)))
    return int((pixels[..., 3] == 0).sum())


def a_place(**overrides) -> dict:
    return {
        "name": "Grandparents flat",
        "category": "family",
        "people": ["Oma", "Opa"],
        "date_from": "1994-06-01",
        "date_to": "1997-08-31",
        "lat": LAT,
        "lon": LON,
        **overrides,
    }


class TestCreating:
    def test_a_place_clears_the_fog_around_it(self, client):
        assert explored(client) == 0

        response = client.post("/api/places", headers=auth(), json=a_place())
        assert response.status_code == 201
        assert explored(client) > 0

    def test_the_fog_it_clears_is_about_thirty_metres_across(self, client):
        client.post("/api/places", headers=auth(), json=a_place())

        # 30 m at this latitude is a hair over 3 px, so a disc of roughly 30 px.
        cleared = explored(client)
        assert 20 < cleared < 45

    def test_it_belongs_to_every_year_it_covers(self, client):
        client.post("/api/places", headers=auth(), json=a_place())

        for year in (1994, 1995, 1996, 1997):
            assert explored(client, f"year:{year}") > 0
        assert explored(client, "year:1998") == 0
        assert explored(client, "year:1993") == 0

    def test_a_place_with_no_dates_goes_to_prehistory(self, client):
        client.post(
            "/api/places",
            headers=auth(),
            json=a_place(date_from=None, date_to=None),
        )
        assert explored(client, "prehistory") > 0

    def test_it_is_recorded_as_an_event_like_everything_else(self, client):
        client.post("/api/places", headers=auth(), json=a_place())

        events = client.get("/api/events?source=place").json()
        assert events["total"] == 1
        assert events["events"][0]["radius_m"] == 30.0
        # A pin clears fog without claiming a route, which is what reveal is.
        assert events["events"][0]["op"] == "reveal"

    def test_creating_needs_the_token(self, client):
        assert client.post("/api/places", json=a_place()).status_code == 401

    @pytest.mark.parametrize(
        "overrides,expected",
        [
            ({"name": "  "}, "A place needs a name"),
            ({"category": "pub"}, "is not one of"),
            ({"lat": 91}, "outside -90 to 90"),
            ({"lon": 181}, "outside -180 to 180"),
            ({"lat": "north"}, "needs numeric lat and lon"),
            ({"date_from": "whenever"}, "is not readable"),
        ],
    )
    def test_bad_input_is_refused_by_name(self, client, overrides, expected):
        response = client.post("/api/places", headers=auth(), json=a_place(**overrides))
        assert response.status_code == 400
        assert expected in response.json()["detail"]


class TestReading:
    def test_places_come_back_with_their_people(self, client):
        client.post("/api/places", headers=auth(), json=a_place())
        body = client.get("/api/places").json()

        assert body["places"][0]["name"] == "Grandparents flat"
        assert body["places"][0]["people"] == ["Oma", "Opa"]
        assert body["people"] == ["Oma", "Opa"]
        assert "family" in body["categories"]

    def test_people_are_deduplicated_and_sorted(self, client):
        client.post(
            "/api/places",
            headers=auth(),
            json=a_place(people=["Opa", "Oma", "Oma", " "]),
        )
        assert client.get("/api/places").json()["places"][0]["people"] == ["Oma", "Opa"]

    def test_people_can_be_given_as_a_comma_separated_string(self, client):
        client.post("/api/places", headers=auth(), json=a_place(people="Oma, Opa"))
        assert client.get("/api/places").json()["places"][0]["people"] == ["Oma", "Opa"]

    def test_filtering_by_person(self, client):
        client.post("/api/places", headers=auth(), json=a_place())
        client.post(
            "/api/places",
            headers=auth(),
            json=a_place(name="School", people=["Frau Huber"], lat=LAT + 0.01),
        )

        oma = client.get("/api/places?person=Oma").json()["places"]
        assert [p["name"] for p in oma] == ["Grandparents flat"]

        huber = client.get("/api/places?person=Frau Huber").json()["places"]
        assert [p["name"] for p in huber] == ["School"]

        assert client.get("/api/places?person=Nobody").json()["places"] == []

    def test_the_filter_ignores_case(self, client):
        client.post("/api/places", headers=auth(), json=a_place())
        assert len(client.get("/api/places?person=oma").json()["places"]) == 1

    def test_reading_needs_no_token(self, client):
        assert client.get("/api/places").status_code == 200


class TestUpdating:
    def test_moving_a_place_moves_the_fog_it_clears(self, client):
        created = client.post("/api/places", headers=auth(), json=a_place()).json()
        assert explored(client) > 0

        client.patch(
            f"/api/places/{created['id']}",
            headers=auth(),
            json={"lat": LAT + 0.05, "lon": LON + 0.05},
        )

        assert explored(client) == 0
        assert explored(client, lat=LAT + 0.05, lon=LON + 0.05) > 0

    def test_changing_the_dates_moves_it_between_years(self, client):
        created = client.post("/api/places", headers=auth(), json=a_place()).json()
        assert explored(client, "year:1995") > 0

        client.patch(
            f"/api/places/{created['id']}",
            headers=auth(),
            json={"date_from": "2010", "date_to": "2011"},
        )

        assert explored(client, "year:1995") == 0
        assert explored(client, "year:2010") > 0

    def test_renaming_leaves_the_fog_where_it_is(self, client):
        created = client.post("/api/places", headers=auth(), json=a_place()).json()
        before = explored(client)

        response = client.patch(
            f"/api/places/{created['id']}", headers=auth(), json={"name": "Oma's flat"}
        )
        assert response.json()["name"] == "Oma's flat"
        assert explored(client) == before

    def test_updating_something_that_is_not_there(self, client):
        response = client.patch("/api/places/4242", headers=auth(), json={"name": "x"})
        assert response.status_code == 404
        assert "No place with id 4242" in response.json()["detail"]

    def test_updating_needs_the_token(self, client):
        assert client.patch("/api/places/1", json={"name": "x"}).status_code == 401


class TestDeleting:
    def test_deleting_a_place_puts_the_fog_back(self, client):
        created = client.post("/api/places", headers=auth(), json=a_place()).json()
        assert explored(client) > 0

        response = client.delete(f"/api/places/{created['id']}", headers=auth())
        assert response.status_code == 200
        assert explored(client) == 0

    def test_its_event_goes_with_it(self, client):
        created = client.post("/api/places", headers=auth(), json=a_place()).json()
        client.delete(f"/api/places/{created['id']}", headers=auth())

        assert client.get("/api/events?source=place").json()["total"] == 0
        assert client.get("/api/places").json()["places"] == []

    def test_deleting_leaves_other_places_alone(self, client):
        first = client.post("/api/places", headers=auth(), json=a_place()).json()
        client.post(
            "/api/places",
            headers=auth(),
            json=a_place(name="School", lat=LAT + 0.05, lon=LON + 0.05),
        )

        client.delete(f"/api/places/{first['id']}", headers=auth())

        assert explored(client) == 0
        assert explored(client, lat=LAT + 0.05, lon=LON + 0.05) > 0
        assert len(client.get("/api/places").json()["places"]) == 1

    def test_deleting_something_that_is_not_there(self, client):
        assert client.delete("/api/places/4242", headers=auth()).status_code == 404

    def test_deleting_needs_the_token(self, client):
        assert client.delete("/api/places/1").status_code == 401


class TestLayerDerivation:
    def test_a_date_range_covers_every_year(self):
        assert places.layers_for("1994-06-01", "1997-08-31") == [
            "1994",
            "1995",
            "1996",
            "1997",
        ]

    def test_bare_years_work_too(self):
        assert places.layers_for("1994", "1995") == ["1994", "1995"]

    def test_one_sided_ranges(self):
        assert places.layers_for("1994", None) == ["1994"]
        assert places.layers_for(None, "1994") == ["1994"]

    def test_no_dates_means_prehistory(self):
        assert places.layers_for(None, None) == ["prehistory"]

    def test_an_unreadable_date_says_what_it_wanted(self):
        with pytest.raises(places.PlaceError, match="Use an ISO date"):
            places.layers_for("last summer", None)
