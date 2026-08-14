# Roadmap

Phases ship in order. Each one is reviewed and tested before the next begins.
Version bumps: patch per commit, minor per completed phase, `1.0.0` when running live on real data.

Current status is in [CHANGELOG.md](CHANGELOG.md).

---

## Phase 0 — Foundation → `0.0.x`

- Repo, Docker Compose, FastAPI skeleton
- SQLite schema: events, blobs, places, settings
- Web-Mercator coordinate math, fully tested
- `selfcheck` command
- Shared-token auth on write endpoints
- `.gitignore`, `.env.example`, versioning

## Phase 1 — Ingest and raster core → `0.1.0`

- GPX and TCX import
- Track segmentation on time/distance gaps
- Accuracy filtering
- Point interpolation
- Brush stamping into z14 bitmaps
- Fog mask and trail pass-count layers
- Tile pyramid build, z14 → z0
- Full rebuild from the event log
- Idempotent re-import

## Phase 2 — Tile server and frontend → `0.2.0`

- PNG tile endpoint, file read only
- Trail intensity colourmap
- Protomaps PMTiles basemap
- MapLibre map: basemap, trails, fog
- Client-side overzoom above z14
- Light and dark themes, UI and map independently

## Phase 2b — Release pipeline → `0.2.x`

- GitHub Actions on `v*` tags
- Images published to GHCR
- Version/tag/changelog consistency checks
- Release notes published from `CHANGELOG.md`
- Production compose file
- README, LICENSE (AGPL-3.0)

## Phase 3 — Time → `0.3.0`

- Year layers derived from timestamps
- Pre-rendered per-year views
- Year slider
- `prehistory` layer for undated data

## Phase 4 — Manual editing → `0.4.0`

- Brush: reveal and erase fog
- Freehand route drawing
- Point-to-point drawing
- Drawing locked to z14 and above
- Client-side point thinning
- Year or year-range assignment per stroke
- Undo via event deletion

## Phase 5 — Places → `0.5.0`

- Named places with category
- Who was there
- Date or date range
- Map markers with popups
- Filter by person
- Places clear fog

## Phase 6 — Live tracking → `0.6.0`

All sources optional, off by default, independently toggleable.

- Overland endpoint, batched, offline-buffer tolerant
- OwnTracks endpoint, HTTP mode
- Home Assistant endpoint via `rest_command`
- Server-side accuracy filtering
- Same-day track appending per source
- Setup docs for all three

## Phase 7 — Workout API sync → `0.7.0`

- Periodic pull from an external workout API
- Dedup by activity ID
- Bulk backfill of existing archive

## Phase 8 — Polish → `0.8.0`

- Soft fog edges
- Vector trail layer above z14, click to identify
- Import progress UI
- Backup guidance

---

## Not planned

Deliberately out of scope. Listed so nobody has to ask.

- User accounts, authentication, multi-user
- Mobile app
- Timeline, visits, trips, automatic place detection
- Per-country statistics
- Route snapping and turn-by-turn routing
- Photo and EXIF import
- Public sharing
- Arbitrary filter UI

## Under consideration

- Wider brush for points tagged as driving
- Collapsing dual-theme rendering to one neutral raster, if MapLibre gains a `raster-color` equivalent
- Fog growth animation across year layers
