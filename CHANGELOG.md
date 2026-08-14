# Changelog

All notable changes to FogMap are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are `MAJOR.MINOR.PATCH` — patch bumps on every code change, minor bumps on phase completion, `1.0.0` when running live on real data.

Entries are written for someone reading the release page, not for someone reading a git log. Describe what changed for the user, not which file was touched.

## [Unreleased]

### Added
- Repository hygiene: data files, secrets and build output are excluded from version control from the first commit onward.
- Web-Mercator coordinate math on the native z14 grid, covering projection round trips, ground resolution by latitude, the Mercator latitude clamp and antimeridian crossings.
- SQLite schema for the event log, raster blobs, places and settings, created on first start and safe to re-run against a populated database.

[Unreleased]: https://github.com/n0ne117/FogMap/commits/main
