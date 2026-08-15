# SPDX-License-Identifier: AGPL-3.0-or-later
"""Surviving the rename from FogMap to Irfaran.

A rename is only free if nobody notices it. An install that was running
happily before the upgrade has a database with the old filename, a .env with
the old variable names, and a tracker app posting the old header - and none
of those are things it should have to fix to keep working.
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from irfaran import db, settings_env
from irfaran.main import LEGACY_TOKEN_HEADER, TOKEN_HEADER, app

TOKEN = "synthetic-rename-token"


class TestEnvironment:
    def test_the_new_prefix_is_read(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_FOG_ALPHA", "200")
        assert settings_env.get("FOG_ALPHA") == "200"

    def test_the_old_prefix_still_works(self, monkeypatch):
        monkeypatch.delenv("IRFARAN_FOG_ALPHA", raising=False)
        monkeypatch.setenv("FOGMAP_FOG_ALPHA", "180")
        assert settings_env.get("FOG_ALPHA") == "180"

    def test_the_new_prefix_wins_when_both_are_set(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_FOG_ALPHA", "200")
        monkeypatch.setenv("FOGMAP_FOG_ALPHA", "180")
        assert settings_env.get("FOG_ALPHA") == "200"

    def test_neither_falls_through_to_the_default(self, monkeypatch):
        monkeypatch.delenv("IRFARAN_FOG_ALPHA", raising=False)
        monkeypatch.delenv("FOGMAP_FOG_ALPHA", raising=False)
        assert settings_env.get("FOG_ALPHA", "fallback") == "fallback"

    def test_an_empty_new_value_does_not_shadow_the_old_one(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_FOG_ALPHA", "   ")
        monkeypatch.setenv("FOGMAP_FOG_ALPHA", "180")
        assert settings_env.get("FOG_ALPHA") == "180"

    def test_the_token_reads_from_either(self, monkeypatch):
        from irfaran import tokens

        monkeypatch.delenv("IRFARAN_TOKEN", raising=False)
        monkeypatch.setenv("FOGMAP_TOKEN", "old-style")
        conn = db.open_initialised(":memory:")
        try:
            assert tokens.resolve(conn) == ("old-style", "environment")
        finally:
            conn.close()


class TestDatabaseFilename:
    def test_an_existing_old_database_is_used_rather_than_orphaned(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path))

        legacy = tmp_path / db.LEGACY_DB_FILENAME
        old = sqlite3.connect(legacy)
        old.execute("CREATE TABLE marker (note TEXT)")
        old.execute("INSERT INTO marker VALUES ('the archive was already here')")
        old.commit()
        old.close()

        assert db.db_path() == legacy

        conn = db.open_initialised()
        try:
            note = conn.execute("SELECT note FROM marker").fetchone()[0]
            assert note == "the archive was already here"
        finally:
            conn.close()
        assert not (tmp_path / db.DB_FILENAME).exists(), "nothing new beside it"

    def test_a_fresh_install_gets_the_new_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path))
        assert db.db_path() == tmp_path / db.DB_FILENAME

    def test_the_new_name_wins_when_both_somehow_exist(self, tmp_path, monkeypatch):
        monkeypatch.setenv("IRFARAN_DATA_DIR", str(tmp_path))
        (tmp_path / db.LEGACY_DB_FILENAME).touch()
        (tmp_path / db.DB_FILENAME).touch()
        assert db.db_path() == tmp_path / db.DB_FILENAME


class TestTokenHeader:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_TOKEN", TOKEN)
        with TestClient(app) as test_client:
            yield test_client

    def test_the_new_header_is_accepted(self, client):
        response = client.post(
            "/api/settings", headers={TOKEN_HEADER: TOKEN}, json={"ui_theme": "dark"}
        )
        assert response.status_code != 401

    def test_the_old_header_is_still_accepted(self, client):
        """A tracker configured months ago does not reconfigure itself."""
        response = client.post(
            "/api/settings",
            headers={LEGACY_TOKEN_HEADER: TOKEN},
            json={"ui_theme": "dark"},
        )
        assert response.status_code != 401

    def test_a_wrong_token_is_still_refused_under_either_name(self, client):
        for header in (TOKEN_HEADER, LEGACY_TOKEN_HEADER):
            response = client.post(
                "/api/events", headers={header: "wrong"}, json={}
            )
            assert response.status_code == 401

    def test_no_header_at_all_is_still_refused(self, client):
        assert client.post("/api/events", json={}).status_code == 401
