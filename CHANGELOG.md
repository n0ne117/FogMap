# Changelog

All notable changes to FogMap are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are `MAJOR.MINOR.PATCH` — patch bumps on every code change, minor bumps on phase completion, `1.0.0` when running live on real data.

Entries are written for someone reading the release page, not for someone reading a git log. Describe what changed for the user, not which file was touched.

## [Unreleased]

Nothing yet.

## [0.9.5] - 2026-08-15

More of the map's own account of itself.

### Added
- The diagnostics panel now reports whether the browser is delivering animation frames, which the map cannot start without, along with the reachability of the sprite and font assets the style waits on.

## [0.9.4] - 2026-08-15

Makes an empty-looking map explain itself.

### Added
- A diagnostics panel under Settings, About, reporting what the map thinks is going on, and map errors are now shown on screen instead of only in the browser console.

## [0.9.3] - 2026-08-15

A fog slider, and room to see what it does.

### Added
- A fog thickness slider in Settings, Appearance, from 0 to 100%. It applies as you drag it — the map is scaled in the browser, so nothing is re-rendered and nothing is requested from the server.

### Changed
- Settings and Places no longer cover the whole map. A margin stays visible all round, so a change can be watched taking effect rather than guessed at.

## [0.9.2] - 2026-08-15

Lets the map show through the fog.

### Fixed
- The map is visible again. Fog over unvisited ground was completely opaque, so with a basemap installed the whole screen was a flat rectangle with the world hidden behind it — no way to navigate, and no way to tell a working install from a broken one. The map now reads through the fog, and how strongly is adjustable.

## [0.9.1] - 2026-08-15

Rescues a basemap download that had already finished.

### Fixed
- A finished basemap download is no longer reported as a failure. The server holds the connection open after the last byte, so waiting for it to close timed out — with all 137 GB already on disk. The next attempt would then have deleted the lot and started again.

## [0.9.0] - 2026-08-14

A reorganised interface, and a fix for pages coming up half empty.

### Fixed
- Per-machine editor and agent settings are now ignored by the repository itself rather than relying on a global ignore file, so the protection travels with a clone.
- Pages no longer come up half empty. Opening FogMap fires several requests at once, and under that load most of them were failing with a server error while the same request made on its own succeeded. Places, markers, data sources and the year slider could all silently fail to appear.

### Changed
- Settings is now organised into tabs, so importing files and configuring data sources each have room to breathe.
- Places has its own page, opened by the pin at the top right of the map.
- One version number in Settings instead of two, linked to the changelog. The api version is mentioned only when it disagrees with the page.
- Protomaps is credited alongside OpenStreetMap and MapLibre in the map attribution.

### Added
- A drawing toolbar on the map, opened by the pencil at the top right, and a vertical zoom control at the left edge.
- An API token field in Settings, Data sources. Everything that changes data needs it, and until now the only place to enter one was the basemap setup screen.

## [0.8.3] - 2026-08-14

A settings screen you can actually read, and proper control over the
basemap download.

### Changed
- Settings is now a full screen of cards instead of a narrow column. The old panel ran off the bottom of the window with no way to scroll, so the data sources and their descriptions could not be reached at all.
- The version badge moved to the bottom left, where it no longer sits on top of the map attribution.

### Added
- Pause, resume and cancel for the basemap download, in Settings. Pausing keeps what has been downloaded so far; cancelling throws it away and asks twice before doing so.

## [0.8.2] - 2026-08-14

Fixes the setup screen refusing to go away.

### Fixed
- Panels that are meant to disappear now actually disappear. "Continue download in the background" and "Continue without a basemap" left the setup screen on top of the map, so both looked like they had done nothing even though the download was running underneath.

## [0.8.1] - 2026-08-14

Setup screen tidying: start the basemap downloading and carry on,
and watch or replace it later from the settings panel.

### Changed
- The setup screen now offers "Continue download in the background", which starts the basemap downloading and gets out of the way. Carrying on without a basemap at all is still there, as a quieter link underneath.

### Added
- Basemap progress, a cancel button and a re-download button now live under Settings, Basemap, so a download can be watched or replaced without reopening the setup screen.

## [0.8.0] - 2026-08-14

Polish. Soft fog edges, clickable tracks when you zoom right in, and
importing from the browser instead of the command line.

Version 0.7.0 is deliberately skipped: phase 7, syncing from a workout app's
API, is deferred until that API is specified.

### Added
- Fog now fades out where it meets ground you have been, instead of stopping at a hard pixel edge.
- Zoomed in past z14, the individual tracks are drawn over the fog and can be clicked to see what they were, when, and from which source.
- Import GPX and TCX files from the web interface, with progress and a per-file result, rather than only from the command line.
- Backup guidance in the README: the event log is the only thing worth keeping, and everything else rebuilds from it.

## [0.6.0] - 2026-08-14

Live tracking. The map can now fill itself in as you go, from
whichever tracker you already use, or from none at all.

### Fixed
- A basemap download now picks itself up after a restart instead of stopping silently, and reports an honest speed and time remaining when it resumes rather than counting bytes fetched days ago against the current run.
- Downloading one of the offered basemaps no longer asks for an API token. Fetching published map data is not a change to your history, and needing a token before the app would fetch its own basemap made the first run harder than it should be. A basemap URL of your own still needs one.

### Added
- Live tracking from Overland, OwnTracks and Home Assistant. All three are off until you switch them on, each independently, and a switched-off endpoint says so rather than quietly swallowing your location. A day of tracking becomes one growing track, and a phone that has been offline can deliver what it recorded in any order.

## [0.5.0] - 2026-08-14

Places. The map now holds the ones you can name, not only the ones a
satellite happened to watch you walk through.

### Added
- Named places. Mark somewhere you lived, went to school or visited, with who was there and when, and the fog clears around it for exactly the years it covers. Markers carry the details, and the map can be filtered down to one person.

## [0.4.0] - 2026-08-14

Manual editing. Draw the routes GPS never recorded, and rub out the
fog it cleared by mistake.

### Added
- Manual editing. Draw a route freehand or point to point, reveal ground you know you have been to, erase fog that GPS drift cleared by mistake, and undo any stroke. A stroke can be assigned to a year or a range of years such as 1994..2002, which writes it to each year it covers.

### Fixed
- Release notes no longer carry a leftover "Nothing yet." placeholder from the unreleased section. Cutting a release is now a command rather than a hand edit, and a release with a placeholder still in it fails to build.

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

[Unreleased]: https://github.com/n0ne117/FogMap/compare/v0.8.3...HEAD
[0.8.3]: https://github.com/n0ne117/FogMap/releases/tag/v0.8.3
[0.8.2]: https://github.com/n0ne117/FogMap/releases/tag/v0.8.2
[0.8.1]: https://github.com/n0ne117/FogMap/releases/tag/v0.8.1
[0.8.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.8.0
[0.6.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.6.0
[0.5.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.5.0
[0.4.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.4.0
[0.3.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.3.0
[0.2.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.2.0
[0.1.0]: https://github.com/n0ne117/FogMap/releases/tag/v0.1.0
