# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conditional requests for tiles.

The tile endpoint always sent an ETag and a Last-Modified and never looked at
either coming back, so a browser revalidating after max-age expired was handed
the whole PNG again. A screenful is fifty-odd tiles; panning over ground
already visited re-fetched all of it every five minutes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from irfaran import composite, db
from irfaran.ingest import common, gpx
from irfaran.main import app, tiles_root

from . import synthetic

TILE = "/api/tiles/dark/all/fog/0/0/0.png"
UNVISITED = "/api/tiles/dark/all/fog/14/1/1.png"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def recoloured(client):
    """Change the fog colour, and put it back afterwards.

    The placeholders live on app.state, which outlives any one test, so leaving
    a different colour behind would quietly change what every later test sees.
    """
    from irfaran.main import app, load_placeholders

    original = None
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'fog_colour_dark'"
        ).fetchone()
        original = row["value"] if row else None
    finally:
        conn.close()

    def apply(colour: str) -> tuple[str, bytes]:
        before = client.get(UNVISITED)
        _set_fog_colour(colour)
        return before.headers["etag"], before.content

    yield apply

    _set_fog_colour(original)


def _set_fog_colour(colour: str | None) -> None:
    from irfaran.main import app, load_placeholders

    conn = db.connect()
    try:
        if colour is None:
            conn.execute("DELETE FROM settings WHERE key = 'fog_colour_dark'")
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES ('fog_colour_dark', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (colour,),
            )
        conn.commit()
        load_placeholders(app, conn)
    finally:
        conn.close()


@pytest.fixture
def rendered(client):
    conn = db.connect()
    try:
        conn.execute("DELETE FROM blobs")
        conn.execute("DELETE FROM events")
        document = synthetic.gpx_document(synthetic.square_loop(40))
        common.ingest_tracks(conn, "workout", gpx.parse(document))
        root = tiles_root()
        composite.write_placeholders(root)
        composite.render_view(conn, root, "all")
    finally:
        conn.close()
    return client


class TestValidators:
    def test_a_tile_carries_an_etag_and_a_date(self, rendered):
        response = rendered.get(TILE)
        assert response.status_code == 200
        assert response.headers["etag"].startswith('"')
        assert response.headers["last-modified"]

    def test_the_same_tile_keeps_the_same_etag(self, rendered):
        first = rendered.get(TILE).headers["etag"]
        assert rendered.get(TILE).headers["etag"] == first


class TestConditionalRequests:
    def test_a_matching_etag_gets_304_with_no_body(self, rendered):
        tag = rendered.get(TILE).headers["etag"]
        response = rendered.get(TILE, headers={"If-None-Match": tag})
        assert response.status_code == 304
        assert not response.content

    def test_a_304_still_carries_the_validators(self, rendered):
        """Without them the next request has nothing to ask with."""
        tag = rendered.get(TILE).headers["etag"]
        response = rendered.get(TILE, headers={"If-None-Match": tag})
        assert response.headers["etag"] == tag
        assert "max-age" in response.headers["cache-control"]

    def test_a_stale_etag_gets_the_tile(self, rendered):
        body = rendered.get(TILE).content
        response = rendered.get(TILE, headers={"If-None-Match": '"stale"'})
        assert response.status_code == 200
        assert response.content == body

    def test_a_wildcard_matches(self, rendered):
        assert rendered.get(TILE, headers={"If-None-Match": "*"}).status_code == 304

    def test_one_of_several_listed_etags_matches(self, rendered):
        tag = rendered.get(TILE).headers["etag"]
        response = rendered.get(TILE, headers={"If-None-Match": f'"other", {tag}'})
        assert response.status_code == 304

    def test_if_modified_since_matches(self, rendered):
        when = rendered.get(TILE).headers["last-modified"]
        assert rendered.get(TILE, headers={"If-Modified-Since": when}).status_code == 304

    def test_an_older_date_gets_the_tile(self, rendered):
        response = rendered.get(
            TILE, headers={"If-Modified-Since": "Thu, 01 Jan 1970 00:00:00 GMT"}
        )
        assert response.status_code == 200

    def test_an_unparseable_date_fetches(self, rendered):
        """Guessing here would serve fog that is out of date."""
        response = rendered.get(TILE, headers={"If-Modified-Since": "whenever"})
        assert response.status_code == 200

    def test_an_etag_beats_a_date(self, rendered):
        """RFC 9110: If-None-Match is exact, so it decides on its own."""
        response = rendered.get(
            TILE,
            headers={
                "If-None-Match": '"stale"',
                "If-Modified-Since": rendered.get(TILE).headers["last-modified"],
            },
        )
        assert response.status_code == 200


class TestUnexploredGround:
    """Most of a fog-of-war map has no file behind it, so this is the common case.

    Placeholders used to go out with no validator at all, which made them the
    one thing a browser could never avoid re-fetching - and there are fifty of
    them on a screen for every rendered tile.
    """

    def test_a_placeholder_carries_an_etag(self, client):
        response = client.get(UNVISITED)
        assert response.status_code == 200
        assert response.headers["etag"].startswith('"')

    def test_a_matching_placeholder_etag_gets_304(self, client):
        tag = client.get(UNVISITED).headers["etag"]
        response = client.get(UNVISITED, headers={"If-None-Match": tag})
        assert response.status_code == 304
        assert not response.content

    def test_a_stale_placeholder_etag_gets_the_fog(self, client):
        response = client.get(UNVISITED, headers={"If-None-Match": '"stale"'})
        assert response.status_code == 200
        assert response.content

    def test_fog_and_trail_placeholders_differ(self, client):
        """One tag for both would serve transparent trail as solid fog."""
        fog = client.get(UNVISITED).headers["etag"]
        trail = client.get("/api/tiles/dark/all/trail/14/1/1.png").headers["etag"]
        assert fog != trail

    def test_the_themes_differ(self, client):
        dark = client.get(UNVISITED).headers["etag"]
        light = client.get("/api/tiles/light/all/fog/14/1/1.png").headers["etag"]
        assert dark != light

    def test_recolouring_the_fog_changes_the_tag(self, client, recoloured):
        """The tag is the content, so a new colour invalidates it by itself."""
        before, body_before = recoloured("#204060")

        after = client.get(UNVISITED)
        assert after.headers["etag"] != before
        assert after.content != body_before
        assert client.get(UNVISITED, headers={"If-None-Match": before}).status_code == 200


class TestCorrectness:

    def test_a_re_rendered_tile_stops_matching(self, rendered):
        """Or a new track would stay invisible until the cache expired.

        Checked on the z14 tile the synthetic loop lands in. At z0 the whole
        world is 256 pixels wide, so a single track changes nothing there - and
        a tile that genuinely did not change is right to keep its ETag.
        """
        from irfaran import geo

        x, y = geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
        deep = f"/api/tiles/dark/all/fog/14/{x}/{y}.png"

        first = rendered.get(deep)
        assert first.status_code == 200
        before = first.headers["etag"]

        # A second track through the same tile, rendered the way the fixture
        # does it, so this exercises the real writer rather than touch(2).
        conn = db.connect()
        try:
            document = synthetic.gpx_document(
                synthetic.straight_line(40, base_lon=synthetic.BASE_LON + 0.002)
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))
            composite.render_view(conn, tiles_root(), "all")
        finally:
            conn.close()

        after = rendered.get(deep).headers["etag"]
        assert after != before
        assert rendered.get(deep, headers={"If-None-Match": before}).status_code == 200
