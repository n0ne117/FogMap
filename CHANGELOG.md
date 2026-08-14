# Changelog

All notable changes to FogMap are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are `MAJOR.MINOR.PATCH` — patch bumps on every code change, minor bumps on phase completion, `1.0.0` when running live on real data.

Entries are written for someone reading the release page, not for someone reading a git log. Describe what changed for the user, not which file was touched.

## [Unreleased]

Nothing yet.

### Fixed
- Release notes no longer carry a leftover "Nothing yet." placeholder from the unreleased section. Cutting a release is now a command rather than a hand edit, and a release with a placeholder still in it fails to build.

### Added
- Manual editing. Draw a route freehand or point to point, reveal ground you know you have been to, erase fog that GPS drift cleared by mistake, and undo any stroke. A stroke can be assigned to a year or a range of years such as 1994..2002, which writes it to each year it covers.

## [0.3.0] - 2026-08-14

Time. The map can be stepped through year by year, or seen all at once.

### Added
- A year slider. Step through the map one year at a time, or see everything at once, with undated routes gathered under their own stop. Every period was rendered at import, so moving the slider is a change of image rather than a wait.

## [0.2.0] - 2026-08-14

The map exists. Import a track and watch the fog lift along it, with the
route shaded by how often you have been that way. Tagged releases now
publish themselves.

### Added
- The map is now drawn. Fog covers ground you have never visited and lifts where you have been, and trails are shaded by how often you passed - a daily commute reads brighter than a one-off detour.
- Tiles are served straight from disk, and a Protomaps basemap can be served from the data directory over range requests.
- A `render` command to rebuild the tile pyramid on demand, reporting how long each view took.
- An actual map. Basemap, trails and fog, with independent light and dark themes for the interface and the map, each remembered between visits.
- First-run setup screen. FogMap now fetches its own basemap - pick one of the recent Protomaps planet builds or give it a URL, and watch the progress. The download resumes if it is interrupted, and the archive is checked before it is installed.
- Tagged releases now build and publish themselves to GitHub Container Registry, with the release notes on the GitHub release page taken straight from this file. A release whose version, tag and notes disagree fails to build rather than shipping.

### Fixed
- A new version of the web interface now reaches the browser on reload instead of being masked by a cached page.

### Changed
- Trails are drawn as a thin line down the middle of a wider cleared corridor, so the map underneath stays readable, and are shaded on a warm ramp from magenta for a single pass to pale yellow for a daily route.

## [0.1.0] - 2026-08-14

First release with a working ingest and raster core. Import a GPX or TCX file
and it is painted into a permanent bitmap; delete every bitmap, rebuild, and
get the same bytes back.

### Added
- Repository hygiene: data files, secrets and build output are excluded from version control from the first commit onward.
- Web-Mercator coordinate math on the native z14 grid, covering projection round trips, ground resolution by latitude, the Mercator latitude clamp and antimeridian crossings.
- SQLite schema for the event log, raster blobs, places and settings, created on first start and safe to re-run against a populated database.
- HTTP API reporting its own version at `/healthz` and `/api/meta`, so the running build can be identified without shell access.
- A `selfcheck` command reporting the running version, every coordinate fixture as expected versus actual, and what is currently in the data directory.
- Web interface showing the running version in the corner, linked to its release notes, alongside the version the API reports.
- Local development stack: one command builds and runs the API and web interface, with a separate service for the test suite.
- Brush stamping: a track is painted into a persistent bitmap once, at import, with the brush width converted from metres to pixels at the latitude of each point.
- Erase now works as a subtract mask applied when a view is drawn, so erased ground stays erased through a rebuild and through re-importing the file that drew the fog underneath it.
- GPX and TCX import. Tracks are split where the trace jumps in time or distance, so a flight no longer draws a line across the map, and importing the same file twice changes nothing.
- Upload endpoints for GPX and TCX files, reporting how many events were created, how many points were stamped and how many tiles changed.
- Command line tools to rebuild every bitmap from the event log, import a file from disk, and dump a single tile to a PNG for inspection. `selfcheck` now reports a digest over all stored bitmaps.

[Unreleased]: https://github.com/n0ne117/FogMap/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.3.0
[0.2.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.2.0
[0.1.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.1.0
