# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parsing, segmentation, filtering and idempotent import."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

import gpxpy
import pytest

from irfaran import db, geo, raster
from irfaran.ingest import common, gpx, tcx

from . import synthetic

UTC = timezone.utc


@pytest.fixture
def conn(tmp_path):
    connection = db.open_initialised(tmp_path / "irfaran.db")
    yield connection
    connection.close()


def fixes_at(seconds: list[float], lon_step: float = 0.0001) -> list[common.Fix]:
    start = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
    return [
        common.Fix(
            lon=synthetic.BASE_LON + index * lon_step,
            lat=synthetic.BASE_LAT,
            time=start + timedelta(seconds=offset),
        )
        for index, offset in enumerate(seconds)
    ]


class TestGpxParsing:
    def test_a_track_parses_into_fixes(self):
        document = synthetic.gpx_document(synthetic.straight_line(10))
        tracks = gpx.parse(document, "synthetic.gpx")

        assert len(tracks) == 1
        assert tracks[0].name == "Synthetic Loop"
        assert len(tracks[0].fixes) == 10
        assert tracks[0].fixes[0].time is not None

    def test_a_track_without_timestamps_still_parses(self):
        document = synthetic.gpx_document(
            synthetic.straight_line(5), with_time=False
        )
        tracks = gpx.parse(document)
        assert all(fix.time is None for fix in tracks[0].fixes)

    def test_bytes_and_text_are_both_accepted(self):
        document = synthetic.gpx_document(synthetic.straight_line(5))
        assert len(gpx.parse(document.encode())[0].fixes) == 5

    def test_a_file_with_no_track_points_says_so(self):
        empty = (
            '<?xml version="1.0"?><gpx version="1.1" creator="t" '
            'xmlns="http://www.topografix.com/GPX/1/1"></gpx>'
        )
        with pytest.raises(ValueError, match="contains no track points"):
            gpx.parse(empty, "empty.gpx")

    def test_unparseable_input_names_the_file(self):
        with pytest.raises(ValueError, match="broken.gpx is not readable as GPX"):
            gpx.parse("this is not xml at all", "broken.gpx")

    def test_a_point_missing_latitude_names_the_file_and_the_problem(self):
        # gpxpy rejects a point with no lat attribute before Irfaran sees it,
        # so the loud message here is the wrapper's: which file, and why.
        document = synthetic.gpx_document(synthetic.straight_line(3), name="Morgenlauf")
        broken = document.replace('lat="0.2500000" lon="0.5001000"', 'lon="0.5001000"')

        with pytest.raises(ValueError) as raised:
            gpx.parse(broken, "morgenlauf.gpx")

        assert "morgenlauf.gpx" in str(raised.value)
        assert "latitude" in str(raised.value)

    def test_a_point_gpxpy_hands_over_without_a_latitude_names_point_and_track(self):
        # The guard behind gpxpy's own validation, for the malformed files it
        # accepts. This is the message the build plan asks for.
        document = synthetic.gpx_document(synthetic.straight_line(3), name="Morgenlauf")
        parsed = gpxpy.parse(document)
        parsed.tracks[0].segments[0].points[1].latitude = None

        with mock.patch.object(gpxpy, "parse", return_value=parsed):
            with pytest.raises(
                ValueError,
                match=r"GPX point 2 missing latitude in track 'Morgenlauf'",
            ):
                gpx.parse(document, "morgenlauf.gpx")


class TestTcxParsing:
    def test_an_activity_parses_into_fixes(self):
        document = synthetic.tcx_document(synthetic.straight_line(12))
        tracks = tcx.parse(document, "synthetic.tcx")

        assert len(tracks) == 1
        assert tracks[0].activity == "Running"
        assert tracks[0].source_id == "2024-03-01T09:00:00Z"
        assert len(tracks[0].fixes) == 12

    def test_trackpoints_without_a_position_are_skipped_not_rejected(self):
        document = synthetic.tcx_document(synthetic.straight_line(5))
        indoor = document.replace(
            "            <Position>\n"
            "              <LatitudeDegrees>0.2500000</LatitudeDegrees>\n"
            "              <LongitudeDegrees>0.5000000</LongitudeDegrees>\n"
            "            </Position>\n",
            "",
            1,
        )
        assert len(tcx.parse(indoor)[0].fixes) == 4

    def test_a_gpx_file_with_a_tcx_extension_says_what_happened(self):
        document = synthetic.gpx_document(synthetic.straight_line(3))
        with pytest.raises(ValueError, match="no <Activity> elements"):
            tcx.parse(document, "mislabelled.tcx")

    def test_unparseable_input_names_the_file(self):
        with pytest.raises(ValueError, match="broken.tcx is not readable as TCX"):
            tcx.parse("<TrainingCenterDatabase><unclosed>", "broken.tcx")


class TestAccuracyFilter:
    def test_fixes_worse_than_the_limit_are_dropped(self):
        fixes = [
            common.Fix(0.5, 0.25, accuracy=5.0),
            common.Fix(0.5, 0.25, accuracy=80.0),
            common.Fix(0.5, 0.25, accuracy=50.0),
        ]
        kept, dropped = common.drop_inaccurate(fixes, limit_m=50.0)
        assert dropped == 1
        assert len(kept) == 2

    def test_fixes_with_no_accuracy_reported_are_kept(self):
        fixes = [common.Fix(0.5, 0.25), common.Fix(0.5, 0.25)]
        kept, dropped = common.drop_inaccurate(fixes, limit_m=50.0)
        assert dropped == 0
        assert len(kept) == 2

    def test_the_default_limit_is_fifty_metres(self):
        assert common.DEFAULT_MAX_ACCURACY_M == 50.0

    def test_the_limit_is_configurable_by_environment(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_MAX_ACCURACY_M", "10")
        assert common.max_accuracy_m() == 10.0

    def test_a_nonsense_setting_is_refused_loudly(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_MAX_ACCURACY_M", "quite accurate")
        with pytest.raises(ValueError, match="IRFARAN_MAX_ACCURACY_M must be a number"):
            common.max_accuracy_m()


class TestSegmentation:
    def test_a_steady_track_stays_in_one_segment(self):
        assert len(common.segment(fixes_at([0, 5, 10, 15]))) == 1

    def test_a_long_pause_splits_the_track(self):
        segments = common.segment(fixes_at([0, 5, 400, 405]), seconds=300)
        assert len(segments) == 2
        assert [len(part) for part in segments] == [2, 2]

    def test_a_long_jump_splits_the_track_even_without_a_pause(self):
        start = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)
        fixes = [
            common.Fix(0.5, 0.25, time=start),
            # Roughly 5000 km away, one second later. This is the flight laser.
            common.Fix(45.0, 0.25, time=start + timedelta(seconds=1)),
        ]
        assert len(common.segment(fixes, metres=1000)) == 2

    def test_undated_fixes_split_on_distance_alone(self):
        fixes = [common.Fix(0.5, 0.25), common.Fix(45.0, 0.25)]
        assert len(common.segment(fixes, metres=1000)) == 2

    def test_thresholds_are_configurable_by_environment(self, monkeypatch):
        monkeypatch.setenv("IRFARAN_GAP_SECONDS", "30")
        monkeypatch.setenv("IRFARAN_GAP_METRES", "250")
        assert common.gap_seconds() == 30.0
        assert common.gap_metres() == 250.0
        assert len(common.segment(fixes_at([0, 5, 100, 105]))) == 2

    def test_an_empty_track_yields_no_segments(self):
        assert common.segment([]) == []


class TestLayerDerivation:
    def test_the_layer_comes_from_the_first_timestamp(self):
        assert common.layer_for(fixes_at([0, 5])) == "2024"

    def test_undated_data_goes_to_prehistory(self):
        assert common.layer_for([common.Fix(0.5, 0.25)]) == "prehistory"

    def test_a_later_timestamp_is_used_when_the_first_fix_has_none(self):
        fixes = [common.Fix(0.5, 0.25)] + fixes_at([0])
        assert common.layer_for(fixes) == "2024"


class TestIdempotentImport:
    def test_importing_the_same_file_twice_creates_no_second_event(self, conn):
        document = synthetic.gpx_document(synthetic.straight_line(30))

        first = common.ingest_tracks(conn, "workout", gpx.parse(document))
        assert first.events_created == 1

        second = common.ingest_tracks(conn, "workout", gpx.parse(document))
        assert second.events_created == 0
        assert second.events_skipped == 1
        assert db.counts(conn)["events"] == 1

    def test_re_import_leaves_the_blobs_byte_identical(self, conn):
        document = synthetic.gpx_document(synthetic.straight_line(30))
        common.ingest_tracks(conn, "workout", gpx.parse(document))
        before = _snapshot(conn)

        common.ingest_tracks(conn, "workout", gpx.parse(document))
        assert _snapshot(conn) == before

    def test_the_same_activity_as_gpx_and_tcx_dedups(self, conn):
        points = synthetic.straight_line(30)
        common.ingest_tracks(conn, "workout", gpx.parse(synthetic.gpx_document(points)))
        result = common.ingest_tracks(
            conn, "workout", tcx.parse(synthetic.tcx_document(points))
        )
        assert result.events_created == 0

    def test_a_different_activity_is_not_deduped(self, conn):
        common.ingest_tracks(
            conn, "workout", gpx.parse(synthetic.gpx_document(synthetic.straight_line(20)))
        )
        later = synthetic.gpx_document(
            synthetic.straight_line(20),
            start=datetime(2024, 3, 2, 9, 0, tzinfo=UTC),
        )
        result = common.ingest_tracks(conn, "workout", gpx.parse(later))
        assert result.events_created == 1
        assert db.counts(conn)["events"] == 2


class TestRebuildIsDeterministic:
    def test_wiping_the_blobs_and_replaying_reproduces_them_exactly(self, conn):
        for day in (1, 2, 3):
            document = synthetic.gpx_document(
                synthetic.square_loop(20),
                start=datetime(2024, 3, day, 9, 0, tzinfo=UTC),
            )
            common.ingest_tracks(conn, "workout", gpx.parse(document))

        before = _snapshot(conn)
        assert before

        replayed, touched = raster.rebuild(conn)
        assert replayed == db.counts(conn)["events"]
        assert touched
        assert _snapshot(conn) == before

    def test_rebuilding_twice_is_stable(self, conn):
        common.ingest_tracks(
            conn, "workout", gpx.parse(synthetic.gpx_document(synthetic.square_loop(15)))
        )
        raster.rebuild(conn)
        once = _snapshot(conn)
        raster.rebuild(conn)
        assert _snapshot(conn) == once

    def test_rebuild_on_an_empty_log_leaves_an_empty_store(self, conn):
        replayed, touched = raster.rebuild(conn)
        assert replayed == 0
        assert touched == set()
        assert db.counts(conn)["blobs"] == 0


class TestIngestResults:
    def test_the_reported_counts_match_what_landed(self, conn):
        document = synthetic.gpx_document(synthetic.straight_line(40))
        result = common.ingest_tracks(conn, "workout", gpx.parse(document))

        assert result.events_created == 1
        assert result.points == 40
        assert result.tiles_touched
        assert result.as_dict()["tiles_touched"] == len(result.tiles_touched)

    def test_touched_tiles_are_the_ones_the_track_actually_covers(self, conn):
        document = synthetic.gpx_document(synthetic.straight_line(40))
        result = common.ingest_tracks(conn, "workout", gpx.parse(document))

        expected = geo.lonlat_to_tile(synthetic.BASE_LON, synthetic.BASE_LAT)
        assert expected in result.tiles_touched

    def test_the_default_radius_comes_from_the_source(self, conn):
        common.ingest_tracks(
            conn, "workout", gpx.parse(synthetic.gpx_document(synthetic.straight_line(5)))
        )
        row = conn.execute("SELECT radius_m FROM events").fetchone()
        assert row["radius_m"] == common.RADIUS_DEFAULTS_M["workout"]

    def test_every_documented_source_has_a_default_radius(self):
        assert common.RADIUS_DEFAULTS_M == {
            "workout": 20.0,
            "ha": 30.0,
            "overland": 20.0,
            "owntracks": 20.0,
            "manual": 15.0,
            "place": 30.0,
        }

    def test_an_unknown_source_is_refused_by_name(self, conn):
        with pytest.raises(ValueError, match="Unknown source 'strava'"):
            common.ingest_tracks(conn, "strava", [])


def _snapshot(conn) -> dict[tuple, bytes]:
    return {
        (row["kind"], row["source"], row["layer"], row["x"], row["y"]): bytes(row["data"])
        for row in conn.execute("SELECT * FROM blobs")
    }
