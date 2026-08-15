# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application-level checks for phase 0.

Covers the two things that would be embarrassing to get wrong: the version
being reachable at runtime, and the shared-token gate actually gating.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fogmap import __version__
from fogmap.main import app

TOKEN = "synthetic-test-token"


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


class TestHealthz:
    def test_reports_ok_and_the_running_version(self, client):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "version": __version__}

    def test_version_looks_like_a_version(self):
        parts = __version__.split(".")
        assert len(parts) == 3
        assert all(part.isdigit() for part in parts)


class TestMeta:
    def test_exposes_version_layers_and_counts(self, client):
        body = client.get("/api/meta").json()
        assert body["version"] == __version__
        assert body["layers"] == []
        assert body["counts"]["events"] == 0
        assert body["counts"]["blobs"] == 0

    def test_seeds_the_default_settings(self, client):
        settings = client.get("/api/meta").json()["settings"]
        assert settings["ha_ingest_enabled"] == "false"
        assert settings["overland_ingest_enabled"] == "false"
        assert settings["owntracks_ingest_enabled"] == "false"
        assert settings["ui_theme"] == "system"
        assert settings["map_theme"] == "dark"


class TestTokenGate:
    """Reads are open. Writes need the shared token."""

    @pytest.mark.parametrize("method", ["post", "put", "patch", "delete"])
    def test_mutations_are_refused_without_a_header(self, client, monkeypatch, method):
        """There is always a token now.

        One is generated on first start, so an unset FOGMAP_TOKEN no longer
        means writes are impossible - it means the generated one is in force.
        A request with no header is therefore unauthorised rather than
        unconfigured.
        """
        monkeypatch.delenv("FOGMAP_TOKEN", raising=False)
        response = getattr(client, method)("/api/events")
        assert response.status_code == 401
        assert "Missing X-FogMap-Token header" in response.json()["detail"]

    def test_missing_header_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
        response = client.post("/api/events")
        assert response.status_code == 401
        assert "Missing X-FogMap-Token header" in response.json()["detail"]

    def test_wrong_token_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
        response = client.post("/api/events", headers={"X-FogMap-Token": "wrong"})
        assert response.status_code == 401
        assert "does not match" in response.json()["detail"]

    def test_correct_token_passes_the_gate(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
        response = client.post("/api/events", headers={"X-FogMap-Token": TOKEN})
        # 422 for the missing body, which is the point: the request got past
        # the middleware and was refused by the route instead.
        assert response.status_code == 422

    def test_the_header_is_matched_case_insensitively(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
        response = client.post("/api/events", headers={"x-fogmap-token": TOKEN})
        assert response.status_code == 422

    @pytest.mark.parametrize("path", ["/healthz", "/api/meta"])
    def test_reads_never_need_a_token(self, client, monkeypatch, path):
        monkeypatch.setenv("FOGMAP_TOKEN", TOKEN)
        assert client.get(path).status_code == 200


class TestDatabase:
    def test_init_is_idempotent(self, tmp_path):
        from fogmap import db

        target = tmp_path / "fogmap.db"
        first = db.open_initialised(target)
        first.execute(
            "INSERT INTO settings (key, value) VALUES ('map_theme_probe', 'kept')"
        )
        first.close()

        second = db.open_initialised(target)
        try:
            settings = db.get_settings(second)
            assert settings["map_theme_probe"] == "kept"
            assert db.counts(second)["events"] == 0
        finally:
            second.close()

    def test_defaults_are_not_reset_on_reinit(self, tmp_path):
        from fogmap import db

        target = tmp_path / "fogmap.db"
        conn = db.open_initialised(target)
        conn.execute("UPDATE settings SET value = 'true' WHERE key = 'ha_ingest_enabled'")
        db.init(conn)
        assert db.get_settings(conn)["ha_ingest_enabled"] == "true"
        conn.close()

    def test_dedup_index_blocks_a_repeated_external_id(self, tmp_path):
        import sqlite3

        from fogmap import db

        conn = db.open_initialised(tmp_path / "fogmap.db")
        row = (
            "workout",
            "add",
            '{"type":"LineString","coordinates":[[0,0],[0.001,0]]}',
            15.0,
            '["2024"]',
            "activity-1",
            "2024-01-01T12:00:00Z",
            None,
        )
        insert = (
            "INSERT INTO events "
            "(source, op, geometry, radius_m, layers, external_id, created_at, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        conn.execute(insert, row)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, row)

        # A NULL external_id is exempt, so manual strokes are never deduped.
        manual = ("manual", "add", row[2], 15.0, '["prehistory"]', None, row[6], None)
        conn.execute(insert, manual)
        conn.execute(insert, manual)
        assert db.counts(conn)["events"] == 3
        conn.close()


class TestConcurrentReads:
    """Every read endpoint must survive a page load.

    A browser opens /api/meta, /api/places and /api/settings at once. FastAPI
    runs the sync connection dependency and the route that uses it in
    whichever worker threads happen to be free, and under load those are
    different threads - which SQLite refuses unless told otherwise. Before
    this was fixed, an ordinary page load returned 500 for most of its
    requests while every one of them succeeded when tried on its own.
    """

    def test_the_read_endpoints_survive_being_called_together(self, client):
        from concurrent.futures import ThreadPoolExecutor

        paths = ["/api/meta", "/api/places", "/api/settings", "/api/setup"] * 6

        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda path: client.get(path), paths))

        failures = [
            (path, response.status_code)
            for path, response in zip(paths, responses)
            if response.status_code != 200
        ]
        assert not failures, f"concurrent reads failed: {failures}"


class TestTokenResolution:
    """A fresh install must not need a token invented before it works."""

    def test_one_is_generated_when_the_environment_has_none(self, tmp_path, monkeypatch):
        from fogmap import db, tokens

        monkeypatch.delenv("FOGMAP_TOKEN", raising=False)
        conn = db.open_initialised(tmp_path / "fogmap.db")
        try:
            token, source = tokens.resolve(conn)
            assert source == "generated"
            assert len(token) >= 32
        finally:
            conn.close()

    def test_the_generated_token_survives_a_restart(self, tmp_path, monkeypatch):
        from fogmap import db, tokens

        monkeypatch.delenv("FOGMAP_TOKEN", raising=False)
        target = tmp_path / "fogmap.db"

        first = db.open_initialised(target)
        token, _ = tokens.resolve(first)
        first.close()

        second = db.open_initialised(target)
        try:
            again, source = tokens.resolve(second)
            assert again == token
            assert source == "generated"
        finally:
            second.close()

    def test_the_environment_wins_over_a_stored_one(self, tmp_path, monkeypatch):
        from fogmap import db, tokens

        monkeypatch.delenv("FOGMAP_TOKEN", raising=False)
        conn = db.open_initialised(tmp_path / "fogmap.db")
        try:
            stored, _ = tokens.resolve(conn)

            monkeypatch.setenv("FOGMAP_TOKEN", "chosen-by-the-operator")
            token, source = tokens.resolve(conn)
            assert token == "chosen-by-the-operator"
            assert source == "environment"
            assert token != stored
        finally:
            conn.close()

    def test_the_setup_endpoint_hands_the_token_over(self, client):
        body = client.get("/api/setup").json()
        assert body["token"]["value"]
        assert body["token"]["source"] in ("environment", "generated")
