# SPDX-License-Identifier: AGPL-3.0-or-later
"""Basemap download, resume and validation.

The download is exercised against a throwaway HTTP server in-process, so
nothing here touches the network.
"""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from fogmap import basemap, db
from fogmap.main import app

# A minimal but structurally valid PMTiles v3 header.
VALID_HEADER = basemap.PMTILES_MAGIC + bytes([3]) + b"\x00" * 119
PAYLOAD = VALID_HEADER + bytes(range(256)) * 40


class RangeHandler(BaseHTTPRequestHandler):
    """Serves one blob, honouring Range so resume can be tested."""

    body = PAYLOAD
    fail_with: int | None = None

    def log_message(self, *args):  # keep the test output clean
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()

    def do_GET(self):
        if self.fail_with:
            self.send_response(self.fail_with)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        requested = self.headers.get("Range")
        if requested:
            start = int(requested.removeprefix("bytes=").split("-")[0])
            chunk = self.body[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range",
                f"bytes {start}-{len(self.body) - 1}/{len(self.body)}",
            )
        else:
            chunk = self.body
            self.send_response(200)

        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/planet.pmtiles"
    httpd.shutdown()


def wait_for(downloader, states=("done", "error", "cancelled"), timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = downloader.status()
        if status["state"] in states:
            return status
        time.sleep(0.05)
    raise AssertionError(f"download never settled, last state {downloader.status()}")


@pytest.fixture
def downloader():
    return basemap.Downloader()


class TestDownload:
    def test_a_complete_download_lands_as_the_real_file(self, downloader, server):
        downloader.start(server, "test-a.pmtiles")
        assert wait_for(downloader)["state"] == "done"

        target = db.data_dir() / "test-a.pmtiles"
        assert target.read_bytes() == PAYLOAD
        assert not (db.data_dir() / "test-a.pmtiles.part").exists()
        target.unlink()

    def test_progress_is_reported_while_it_runs(self, downloader, server):
        downloader.start(server, "test-b.pmtiles")
        status = wait_for(downloader)

        assert status["total_bytes"] == len(PAYLOAD)
        assert status["downloaded_bytes"] == len(PAYLOAD)
        assert status["percent"] == 100.0
        (db.data_dir() / "test-b.pmtiles").unlink()

    def test_an_interrupted_download_resumes_instead_of_restarting(
        self, downloader, server
    ):
        partial = db.data_dir() / "test-c.pmtiles.part"
        partial.write_bytes(PAYLOAD[:100])

        downloader.start(server, "test-c.pmtiles")
        assert wait_for(downloader)["state"] == "done"

        target = db.data_dir() / "test-c.pmtiles"
        # Resumed rather than appended: the file is not 100 bytes too long.
        assert target.read_bytes() == PAYLOAD
        target.unlink()

    def test_two_downloads_at_once_are_refused(self, downloader, server):
        downloader.start(server, "test-d.pmtiles")
        with pytest.raises(RuntimeError, match="already running"):
            downloader.start(server, "test-e.pmtiles")

        wait_for(downloader)
        (db.data_dir() / "test-d.pmtiles").unlink(missing_ok=True)

    def test_a_failed_request_is_reported_not_swallowed(self, downloader, server):
        RangeHandler.fail_with = 500
        try:
            downloader.start(server, "test-f.pmtiles")
            status = wait_for(downloader)
            assert status["state"] == "error"
            assert status["error"]
        finally:
            RangeHandler.fail_with = None


class TestResumeAfterRestart:
    """A download that a restart cut off has to pick itself up.

    This exists because it did not: rebuilding the api container during
    development silently stopped a 137 GB download, and nothing said so.
    """

    def test_an_interrupted_download_is_reported_as_interrupted_not_idle(
        self, downloader, server
    ):
        downloader.start(server, "test-h.pmtiles")
        wait_for(downloader)
        (db.data_dir() / "test-h.pmtiles").unlink(missing_ok=True)

        # A fresh Downloader is what a restarted process gets.
        restarted = basemap.Downloader()
        restarted._state_path().write_text(
            '{"url": "http://example.invalid/x.pmtiles", "filename": '
            '"test-i.pmtiles", "state": "running", "total_bytes": 100, '
            '"downloaded_bytes": 10}',
            encoding="utf-8",
        )
        assert restarted.status()["state"] == "interrupted"

    def test_it_starts_again_on_its_own(self, server):
        restarted = basemap.Downloader()
        restarted._state_path().write_text(
            f'{{"url": "{server}", "filename": "test-j.pmtiles", '
            '"state": "running", "total_bytes": 100, "downloaded_bytes": 10}',
            encoding="utf-8",
        )

        assert restarted.resume_if_interrupted() is True
        assert wait_for(restarted)["state"] == "done"
        assert (db.data_dir() / "test-j.pmtiles").read_bytes() == PAYLOAD
        (db.data_dir() / "test-j.pmtiles").unlink()

    def test_a_finished_download_is_not_started_again(self, server, downloader):
        target = db.data_dir() / "test-k.pmtiles"
        target.write_bytes(PAYLOAD)
        try:
            restarted = basemap.Downloader()
            restarted._state_path().write_text(
                f'{{"url": "{server}", "filename": "test-k.pmtiles", '
                '"state": "running", "total_bytes": 100, "downloaded_bytes": 100}',
                encoding="utf-8",
            )
            assert restarted.resume_if_interrupted() is False
        finally:
            target.unlink()

    def test_a_cancelled_download_stays_cancelled(self, server):
        restarted = basemap.Downloader()
        restarted._state_path().write_text(
            f'{{"url": "{server}", "filename": "test-l.pmtiles", '
            '"state": "cancelled"}',
            encoding="utf-8",
        )
        assert restarted.resume_if_interrupted() is False

    def test_nothing_to_resume_is_not_an_error(self):
        fresh = basemap.Downloader()
        fresh._state_path().unlink(missing_ok=True)
        assert fresh.resume_if_interrupted() is False


class TestValidation:
    def test_a_valid_header_is_accepted(self, downloader, tmp_path):
        archive = tmp_path / "ok.pmtiles"
        archive.write_bytes(VALID_HEADER)
        downloader._verify(archive)

    def test_an_error_page_saved_as_pmtiles_is_rejected(self, downloader, tmp_path):
        archive = tmp_path / "bad.pmtiles"
        archive.write_bytes(b"<!doctype html><title>404 Not Found</title>")

        with pytest.raises(ValueError, match="is not a PMTiles archive"):
            downloader._verify(archive)

    def test_the_wrong_pmtiles_version_is_rejected(self, downloader, tmp_path):
        archive = tmp_path / "old.pmtiles"
        archive.write_bytes(basemap.PMTILES_MAGIC + bytes([2]) + b"\x00" * 119)

        with pytest.raises(ValueError, match="is PMTiles version 2"):
            downloader._verify(archive)

    def test_a_download_that_is_not_pmtiles_never_becomes_the_basemap(
        self, downloader, server
    ):
        RangeHandler.body = b"not a pmtiles archive at all"
        try:
            downloader.start(server, "test-g.pmtiles")
            assert wait_for(downloader)["state"] == "error"
            assert not (db.data_dir() / "test-g.pmtiles").exists()
        finally:
            RangeHandler.body = PAYLOAD
            (db.data_dir() / "test-g.pmtiles.part").unlink(missing_ok=True)


class TestSuggestedUrls:
    def test_recent_daily_builds_are_offered_newest_first(self):
        urls = basemap.suggested_planet_urls("20260814", days=3)
        assert urls == [
            "https://build.protomaps.com/20260814.pmtiles",
            "https://build.protomaps.com/20260813.pmtiles",
            "https://build.protomaps.com/20260812.pmtiles",
        ]

    def test_it_crosses_a_month_boundary(self):
        assert "20260731" in basemap.suggested_planet_urls("20260801", days=2)[1]


class TestSetupEndpoint:
    @pytest.fixture
    def client(self):
        with TestClient(app) as test_client:
            yield test_client

    def test_status_is_readable_without_a_token(self, client):
        body = client.get("/api/setup").json()
        assert body["basemap"]["filename"] == "planet.pmtiles"
        assert body["suggested_urls"]

    def test_a_url_of_your_own_needs_the_token(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        response = client.post(
            "/api/setup/basemap", json={"url": "https://example.org/a.pmtiles"}
        )
        assert response.status_code == 401
        assert "points this server at an address you supplied" in (
            response.json()["detail"]
        )

    def test_an_offered_build_needs_no_token(self, client, monkeypatch):
        """First run should not send anyone hunting for a token in a .env file.

        Fetching a published basemap from a known source downloads public map
        data into a cache. It changes nobody's history, which is what the
        token is there to protect.
        """
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        suggested = client.get("/api/setup").json()["suggested_urls"][0]

        response = client.post("/api/setup/basemap", json={"url": suggested})
        assert response.status_code == 200
        basemap.downloader.cancel()

    def test_cancelling_needs_no_token_either(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        assert client.delete("/api/setup/basemap").status_code == 200

    @pytest.mark.parametrize(
        "url",
        [
            "http://build.protomaps.com/20260814.pmtiles",  # not https
            "https://build.protomaps.com.evil.test/x.pmtiles",  # lookalike host
            "https://evil.test/?u=https://build.protomaps.com/x.pmtiles",
        ],
    )
    def test_only_the_real_host_over_https_is_trusted(self, url):
        from fogmap.main import is_trusted_basemap

        assert is_trusted_basemap(url) is False

    def test_the_real_host_is_trusted(self):
        from fogmap.main import is_trusted_basemap

        assert is_trusted_basemap("https://build.protomaps.com/20260814.pmtiles")

    def test_a_non_http_url_is_refused(self, client, monkeypatch):
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        response = client.post(
            "/api/setup/basemap",
            json={"url": "file:///etc/passwd"},
            headers={"X-FogMap-Token": "a-token"},
        )
        assert response.status_code == 400
        assert "is not an http or https URL" in response.json()["detail"]

    def test_a_filename_that_escapes_the_data_directory_is_refused(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("FOGMAP_TOKEN", "a-token")
        response = client.post(
            "/api/setup/basemap",
            json={"url": "https://example.org/a.pmtiles", "filename": "../evil.pmtiles"},
            headers={"X-FogMap-Token": "a-token"},
        )
        assert response.status_code == 400
        assert "not a valid PMTiles filename" in response.json()["detail"]


class TestDownloadStateMaths:
    def test_speed_and_remaining_time_are_derived_from_progress(self):
        state = basemap.DownloadState(
            state="running",
            total_bytes=1000,
            downloaded_bytes=250,
            started_at=100.0,
            updated_at=110.0,
        )
        reported = state.as_dict()

        assert reported["percent"] == 25.0
        assert reported["bytes_per_second"] == 25
        assert reported["seconds_remaining"] == 30

    def test_speed_after_a_resume_counts_only_this_run(self):
        # 200 bytes already on disk, 50 fetched in 10 seconds. Reporting 25 B/s
        # here would be wrong by a factor of five, and the ETA with it.
        state = basemap.DownloadState(
            state="running",
            total_bytes=1000,
            downloaded_bytes=250,
            resumed_from=200,
            started_at=100.0,
            updated_at=110.0,
        )
        reported = state.as_dict()

        assert reported["bytes_per_second"] == 5
        assert reported["seconds_remaining"] == 150

    def test_an_unstarted_download_reports_no_estimate(self):
        reported = basemap.DownloadState().as_dict()
        assert reported["percent"] == 0.0
        assert reported["seconds_remaining"] is None
