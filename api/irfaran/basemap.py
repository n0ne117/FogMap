# SPDX-License-Identifier: AGPL-3.0-or-later
"""Basemap archive download and validation.

A planet PMTiles archive is well over a hundred gigabytes, which means the
download is measured in hours and will be interrupted. So it resumes: bytes
land in a .part file, and restarting picks up from wherever it stopped using
an HTTP range request rather than starting again.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from irfaran import db

# PMTiles v3 archives begin with this magic, so a wrong URL is caught before
# it has been written over a good archive.
PMTILES_MAGIC = b"PMTiles"
PMTILES_VERSION = 3
HEADER_BYTES = 127

CHUNK = 1024 * 1024
STATE_FILE = "basemap-download.json"
USER_AGENT = "Irfaran basemap downloader"

# Protomaps publishes a daily planet build and keeps roughly the last week.
PLANET_URL_TEMPLATE = "https://build.protomaps.com/{date}.pmtiles"


@dataclass
class DownloadState:
    url: str = ""
    filename: str = ""
    state: str = "idle"  # idle | running | done | error | cancelled
    total_bytes: int = 0
    downloaded_bytes: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0
    error: str = ""
    # Bytes already on disk when this run began. Speed is measured from here,
    # not from zero - otherwise resuming a 13 GB partial reports a couple of
    # gigabytes a second and an ETA of nothing.
    resumed_from: int = 0

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = dict(asdict(self))
        elapsed = max(self.updated_at - self.started_at, 1e-6)
        this_run = max(self.downloaded_bytes - self.resumed_from, 0)
        speed = this_run / elapsed if self.state == "running" else 0.0
        remaining = max(self.total_bytes - self.downloaded_bytes, 0)

        out["percent"] = (
            round(self.downloaded_bytes / self.total_bytes * 100, 2)
            if self.total_bytes
            else 0.0
        )
        out["bytes_per_second"] = round(speed)
        out["seconds_remaining"] = round(remaining / speed) if speed > 0 else None
        return out


class Downloader:
    """Runs one basemap download at a time, in a background thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state = DownloadState()
        self._loaded = False

    # -- paths ---------------------------------------------------------------

    def target(self, filename: str) -> Path:
        return db.basemap_dir() / filename

    def partial(self, filename: str) -> Path:
        return db.basemap_dir() / f"{filename}.part"

    def _state_path(self) -> Path:
        return db.data_dir() / STATE_FILE

    # -- state ---------------------------------------------------------------

    def _persist(self) -> None:
        try:
            self._state_path().write_text(
                json.dumps(asdict(self._state)), encoding="utf-8"
            )
        except OSError:
            # Losing the progress record is not worth failing the download for.
            pass

    def _restore(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            stored = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        known = {f for f in DownloadState.__dataclass_fields__}
        self._state = DownloadState(**{k: v for k, v in stored.items() if k in known})
        if self._state.state == "running":
            # Nothing is running after a restart, whatever the file says. This
            # is distinct from idle: it means a download was cut off mid-flight
            # and should be picked up again, not that nothing was happening.
            self._state.state = "interrupted"

    def status(self) -> dict[str, object]:
        with self._lock:
            self._restore()
            return self._state.as_dict()

    # -- control -------------------------------------------------------------

    def start(self, url: str, filename: str) -> dict[str, object]:
        with self._lock:
            self._restore()
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError(
                    "A basemap download is already running. Cancel it before "
                    "starting another."
                )

            self._cancel.clear()
            self._state = DownloadState(
                url=url,
                filename=filename,
                state="running",
                started_at=time.time(),
                updated_at=time.time(),
            )
            self._persist()
            self._thread = threading.Thread(
                target=self._run, args=(url, filename), daemon=True
            )
            self._thread.start()
            return self._state.as_dict()

    def resume_if_interrupted(self) -> bool:
        """Pick up a download that a restart cut off.

        A planet archive takes hours, so a container restart during one is not
        an edge case - it is the normal way it ends. Without this the download
        simply stops and nothing says so, which is exactly what happened while
        the containers were being rebuilt during development.
        """
        with self._lock:
            self._restore()
            state = self._state
            if state.state != "interrupted" or not state.url or not state.filename:
                return False
            # A partial file means a download was in flight. During an update
            # the previous archive is still installed, so the target existing
            # is not on its own a reason to stop.
            if (
                self.target(state.filename).exists()
                and not self.partial(state.filename).exists()
            ):
                return False

        self.start(state.url, state.filename)
        return True

    def cancel(self, discard: bool = False) -> dict[str, object]:
        """Stop the download.

        Pausing keeps the partial file, so resuming picks up where it stopped -
        which after several hours and tens of gigabytes is the difference
        between an interruption and a disaster. Discarding throws those bytes
        away and is the only way to start over from nothing.
        """
        self._cancel.set()

        thread = self._thread
        if discard and thread is not None and thread.is_alive():
            # Wait for the writer to let go before removing the file.
            thread.join(timeout=10.0)

        with self._lock:
            if self._state.state == "running":
                self._state.state = "cancelled" if discard else "paused"

            if discard:
                filename = self._state.filename
                if filename:
                    self.partial(filename).unlink(missing_ok=True)
                self._state = DownloadState(
                    url=self._state.url, filename=filename, state="cancelled"
                )

            self._persist()
            return self._state.as_dict()

    # -- the work ------------------------------------------------------------

    def _run(self, url: str, filename: str) -> None:
        partial = self.partial(filename)
        target = self.target(filename)

        try:
            # The basemap can live on a different disk from everything else,
            # and that mount may be empty on first run.
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise RuntimeError(
                    f"Cannot write the basemap to {target.parent}. Check that "
                    "the directory exists and is writable - on Docker, that "
                    "the volume is mounted there."
                ) from exc

            already = partial.stat().st_size if partial.exists() else 0
            total = self._remote_size(url)

            # Every byte is already here. This happens when a previous attempt
            # downloaded the lot and then died on the way out - finish it
            # rather than fetching a hundred gigabytes again.
            if already and total and already == total:
                self._finish(partial, target)
                return

            if already and total and already > total:
                # Longer than the archive is: genuinely wrong, start over.
                already = 0
                partial.unlink()

            with self._lock:
                self._state.total_bytes = total
                self._state.downloaded_bytes = already
                self._state.resumed_from = already
                self._state.started_at = time.time()
                self._persist()

            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            if already:
                request.add_header("Range", f"bytes={already}-")

            with urllib.request.urlopen(request, timeout=60) as response:
                if already and response.status != 206:
                    # The server ignored the range, so start over rather than
                    # append the whole file onto a partial one.
                    already = 0
                    partial.unlink(missing_ok=True)

                mode = "ab" if already else "wb"
                with partial.open(mode) as handle:
                    written = already
                    last_report = 0.0
                    while not self._cancel.is_set():
                        # Stop on the byte count rather than waiting for an
                        # empty read. Protomaps holds the connection open after
                        # the last byte, so reading once more blocks until the
                        # socket times out - and a complete download was being
                        # reported as a failure because of it.
                        if total and written >= total:
                            break

                        chunk = response.read(CHUNK)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)

                        now = time.time()
                        if now - last_report > 1.0:
                            last_report = now
                            with self._lock:
                                self._state.downloaded_bytes = written
                                self._state.updated_at = now
                                self._persist()

            if self._cancel.is_set():
                with self._lock:
                    self._state.state = "cancelled"
                    self._state.updated_at = time.time()
                    self._persist()
                return

            self._finish(partial, target)

        except Exception as exc:  # surfaced to the user, never swallowed
            # A stalled read at the very end must not throw away a download
            # that already has every byte of the archive.
            try:
                if partial.exists() and partial.stat().st_size >= self._state.total_bytes > 0:
                    self._finish(partial, target)
                    return
            except Exception:
                pass

            with self._lock:
                self._state.state = "error"
                self._state.error = f"{type(exc).__name__}: {exc}"
                self._state.updated_at = time.time()
                self._persist()

    def _finish(self, partial: Path, target: Path) -> None:
        """Check the archive and put it in place."""
        self._verify(partial)
        partial.replace(target)

        with self._lock:
            self._state.downloaded_bytes = target.stat().st_size
            self._state.total_bytes = self._state.downloaded_bytes
            self._state.state = "done"
            self._state.error = ""
            self._state.updated_at = time.time()
            self._persist()

    def _remote_size(self, url: str) -> int:
        request = urllib.request.Request(
            url, method="HEAD", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return int(response.headers.get("Content-Length") or 0)
        except (urllib.error.URLError, ValueError):
            return 0

    def _verify(self, path: Path) -> None:
        """Reject anything that is not a PMTiles v3 archive.

        A 404 page saved under the right filename would otherwise sit there
        looking like a basemap until the map failed to draw.
        """
        with path.open("rb") as handle:
            header = handle.read(HEADER_BYTES)

        if not header.startswith(PMTILES_MAGIC):
            raise ValueError(
                f"{path.name} is not a PMTiles archive - it does not start with "
                f"the PMTiles magic bytes. The URL probably returned an error "
                f"page. First bytes were {header[:16]!r}."
            )
        version = header[7]
        if version != PMTILES_VERSION:
            raise ValueError(
                f"{path.name} is PMTiles version {version}, and Irfaran serves "
                f"version {PMTILES_VERSION}."
            )


downloader = Downloader()


def installed_path(filename: str = "planet.pmtiles") -> Path:
    """Where the archive is, whether or not it has been downloaded yet.

    Callers that need to read it check `is_file()` themselves - a gazetteer build
    asked for on a machine with no basemap should say so rather than raise.
    """
    return db.basemap_dir() / filename


def basemap_status(filename: str = "planet.pmtiles") -> dict[str, object]:
    """What the setup screen needs to know about the basemap."""
    target = db.basemap_dir() / filename
    partial = db.basemap_dir() / f"{filename}.part"

    present = target.is_file()
    return {
        "filename": filename,
        "present": present,
        "bytes": target.stat().st_size if present else 0,
        "partial_bytes": partial.stat().st_size if partial.is_file() else 0,
        "path": str(target),
        "download": downloader.status(),
    }


def suggested_planet_urls(today: str, days: int = 6) -> list[str]:
    """Recent Protomaps daily builds, newest first.

    They are kept for about a week, so the most recent few are offered rather
    than one date that may already have expired.
    """
    from datetime import date, timedelta

    parsed = date(int(today[0:4]), int(today[4:6]), int(today[6:8]))
    return [
        PLANET_URL_TEMPLATE.format(date=(parsed - timedelta(days=offset)).strftime("%Y%m%d"))
        for offset in range(days)
    ]
