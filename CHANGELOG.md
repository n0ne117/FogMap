# Changelog

All notable changes to Irfaran are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are `MAJOR.MINOR.PATCH` — patch bumps on every code change, minor bumps on phase completion, `1.0.0` when running live on real data.

Entries are written for someone reading the release page, not for someone reading a git log. Describe what changed for the user, not which file was touched.

## [Unreleased]

Nothing yet.

## [0.10.5] - 2026-08-16

### Fixed
- **The API token was readable by anyone who could reach the app.** `/api/setup` is documented as readable without a token, and it included the token itself — so on an instance anyone could open, anyone could read the write key and then draw, delete or wipe. Reads being open is a deliberate choice; handing out the key to change things was not. The token is now served only during genuine first-run setup, and only when Irfaran generated it: a token set through `IRFARAN_TOKEN` was chosen by the operator, who already has it, so printing it back leaks it for nothing.

### Added
- Setup has two screens instead of one. The first run shows the token once and says so. Every browser after that is told the server is already set up and asked to paste the token to enable editing, or to carry on reading the map, which never needed one. A pasted token is checked against the server before it is believed, rather than being stored and failing silently at the first edit.
- `python -m irfaran.cli token` prints the token from the console. Once setup is finished that is the only way to read it back — and the console is the one place where being able to read it already means you own the machine.

Finishing setup is itself an authenticated call, which is the right proof: to say you have the token, present it. If it fails, setup stays open for someone who can.

## [0.10.4] - 2026-08-16

### Fixed
- Installing from the published images failed on the documented first run. `docker-compose.prod.yml` still required `IRFARAN_TOKEN` to be set, from before Irfaran generated one for you — so leaving it blank, which the guide says is the recommended way to start, aborted with an error naming `FOGMAP_TOKEN`, a variable the reader has never heard of. Blank is now allowed and the app generates a token as documented.

Found by following the install guide from a clean directory with nothing but the Compose file and a `.env`, which is the only way that class of mistake shows up.

## [0.10.3] - 2026-08-16

A website, and somewhere else to put the basemap.

### Added
- A one-page site in `docs/`, ready for GitHub Pages, with install guides for Docker Compose and for unraid's Docker Compose Manager. Every image on it is rendered from invented data — a seeded random walk on a street grid in the middle of the Atlantic — by two scripts kept beside them, because a fog-of-war map of somebody's life is a map of where they live, work and run.
- `IRFARAN_BASEMAP_HOST` puts the basemap somewhere other than the data directory. The planet archive is around 137 GB of public map data that can always be fetched again; everything else in there is irreplaceable and measured in megabytes. On a NAS they belong on different disks, and until now they could not be.

### Changed
- `.env.example` documents `COMPOSE_PROJECT_NAME`. Compose names a stack after the folder it sits in, so two checkouts in folders both called Irfaran are one stack as far as it is concerned, and starting either replaces the other's containers. Found by walking through the new install guide on a clean clone, which promptly swapped out the running instance.

## [0.10.2] - 2026-08-15

### Fixed
- Single mode barely deduplicated anything. It kept any track covering 30% new ground, on a 25 m grid with no tolerance for GPS scatter — so two traces of one street landing either side of a grid line each looked like new ground and both were drawn. The grid is 10 m now, a cell counts as covered when any neighbouring cell is, and a track needs 15% genuinely new ground to earn a line. Two runs down the same street are one route; a run that shares most of its length but takes a detour is still drawn, and only the detour counts as newly covered.

### Changed
- Recolouring the trails no longer re-renders the fog, and recolouring the fog no longer re-renders the trails. Neither changes a single pixel of the other. On this archive a trail recolour went from 12 minutes to 6, and the progress became close to linear as a side effect — the fog and trail passes have very different costs, and interleaving them was most of why the percentage appeared to stall and then leap.
- Recolouring says up front that it re-renders every tile and takes several minutes, then reports a live estimate rather than a bare percentage. The estimate projects from the rate observed so far, so it corrects itself as the work turns out to be faster or slower than it looked.
- The controls that can start a render — both colour pickers and the import button — are locked while one is running. Starting a second render over a half-finished one leaves the tiles in a state neither of them intended.

## [0.10.1] - 2026-08-15

### Fixed
- The map rendered nothing at all — no basemap, no fog, no trails. The trail strength slider added in 0.9.18 multiplied the trail layer's zoom fade by a factor, and MapLibre requires a `zoom` expression to be the input of a *top-level* interpolate. Nesting one inside a multiply makes the style invalid, and an invalid style is rejected whole rather than layer by layer, so one bad paint property took the entire map down. The strength is now folded into the interpolation's output values, which is the same curve expressed legally.

This was introduced in 0.9.18 and survived the rename in 0.10.0 unchanged; the rename had nothing to do with it.

## [0.10.0] - 2026-08-15

The project is called Irfaran.

*Irfaran* is Old High German for travelling through a thing and so coming to know it — *ir-* on *faran*, to go, which became *erfahren*, to experience, and *Erfahrung*, experience itself. A map that stays under fog until you have been there does not record where you went; it records what you have come to know by going. It was called FogMap until now, and that name was taken.

### Changed
- Every FogMap became Irfaran: the Python package, the container images, the API token header, the environment variables, the settings the browser remembers, and the map layers.

Nothing an existing install depends on breaks, because none of the old names were simply dropped:

- **The database keeps its filename.** An install that already has `fogmap.db` goes on using it. Renaming somebody's archive to tidy up a filename is not a trade worth making, and a fresh empty database beside a full one looks exactly like losing everything.
- **`FOGMAP_` environment variables still work.** `IRFARAN_` wins where both are set, so a `.env` can be updated whenever it suits rather than before the next restart.
- **The `X-FogMap-Token` header is still accepted.** A tracker configured months ago does not reconfigure itself, and an install that starts rejecting its own tracker on upgrade is worse than a spelling nobody sees.
- **Browser settings are carried over on first load**, the API token among them. Losing that would look identical to being locked out.
- **Compose forwards both spellings.** The code inside the container can be as forgiving as it likes; a variable Compose never passes in is a variable that is simply not there.

## [0.9.18] - 2026-08-15

The trail colouring gets its own controls, and stops being drowned out.

### Added
- **Single** joins the track line modes: one line per route rather than per journey. Tracks covering ground an earlier one already covered are dropped, so a commute walked four hundred times is drawn once. How often you went is what the trail colouring is for; drawing the line four hundred times says the same thing illegibly.
- **Trail colouring** in Settings, Appearance: a strength slider and four colour sets — ember, ice, moss and mono. Strength is applied in the browser and changes as you drag; the colours are baked into the tiles, so changing those re-renders them with a progress readout.

### Changed
- The trail colouring is feathered at the deep zoom levels. On the native grid a track is one pixel and softening it would erase it; two levels down it is a four-pixel stripe with visibly stepped diagonals, which reads as a bar chart rather than as heat. The feathering only ever adds glow around a track, never takes brightness off one.

### Fixed
- "Showing the first 500 tracks here" no longer appears on every viewport. Whether a track was in view was decided by whether its *bounding box* overlapped the screen — and a ten kilometre run across a city has a bounding box covering the city, so every run in the archive qualified for every viewport in it. The cap was hit every single time and the browser was handed five hundred tracks mostly nowhere near what was on screen.
- Changing the fog or trail colours no longer blocks for minutes and time out at the proxy. Both now mark the tiles as owing a render and return immediately, and the render runs through the same reporting path as a bulk import.

## [0.9.17] - 2026-08-15

Somewhere you go every day stops erasing itself.

### Added
- **Track lines** in Settings, Appearance: Auto, Detailed, Faint or Off. Detailed is what shipped before — one legible line per track with a casing under it, which is right until a hundred of them share a street and the whole neighbourhood turns into a white slab. Faint draws hairlines at low opacity with no casing, so overlapping tracks add up instead of covering each other: a street crossed once is a whisper, a street crossed every morning is bright. Auto uses whichever suits how much is on screen. The trail colouring underneath is untouched in every mode.
- Clicking a track to see its details is now a setting, on by default, in the same place.

Both are viewing choices applied in the browser, so switching between them is instant and nothing is re-rendered or re-fetched.

## [0.9.16] - 2026-08-15

Places, properly.

### Added
- Places is a sidebar rather than a sheet. A sheet covers the middle of the map, which is exactly where you are trying to drop a pin.
- **Drop a pin** turns the cursor into a map pin and puts one wherever you click. It is draggable until you save it, because nobody lands on the right pixel first time, and Escape gets you out without saving anything.
- A pin has a title, a label, tags and a folder. Dropping one clears 30 m of fog around it — as a reveal, so it clears the ground without drawing a route through it.
- **Labels** are defined in Settings, Labels: a name and a colour, with the same wheel-and-hex picker as the fog. A pin wears its label's colour on the map. Deleting a label leaves its pins exactly where they are and only takes the colour away.
- **Folders**, nesting two deep, each with an eye that hides everything filed under it — including through a subfolder. Deleting a folder is a filing decision: its subfolders go, its pins come back out as unfiled, and nothing on the map is removed.
- Clicking a pin shows its title, label, folder, tags and coordinates, with Edit and Delete.

### Fixed
- Creating, moving and deleting a place used to re-render every tile of every affected view. On an archive of any size that was minutes; a pin now takes about five seconds, scoped to the ground it covers.
- Moving a place from one year to another no longer leaves the year it came from showing fog that is not there any more. Both the old and new years are re-rendered, and a year emptied entirely retires along with its tiles.

## [0.9.15] - 2026-08-15

Two tools for ground you were around rather than ground you walked along.

### Added
- **Reveal** clears fog and leaves no track. The brush has always done both at once, which quietly asserts a route — and "I lived in this village for six years" is not a route. Reveal is the same brush without that claim.
- **Area** encloses somewhere and clears all of it. Click round the edge, double click to close. A week on an island is not a stroke of any width, and dragging a 60 m brush across a town to say so was never going to be the answer. Areas are stored as GeoJSON polygons and filled; the ring itself is stamped with the brush, so the boundary is as round as a drawn one.
- Neither tool appears in the track layer, because neither is a track.

### Changed
- The brush is called **Track** now, since it is the one that draws one.
- Removed ROADMAP.md. It described a plan the project has long since diverged from, and a roadmap nobody is following is worse than no roadmap.

## [0.9.14] - 2026-08-15

The progress bar keeps working after the last file lands.

### Changed
- The import progress bar restarts from zero when the render begins and tracks that instead. Importing is now the quick half; drawing the map is what there is to wait for, and the bar was sitting full through all of it. The render reports each finished piece of work as it lands, so the bar means something the whole way rather than being a spinner in a bar's clothing.
- /api/render answers with newline-delimited JSON — a line per finished unit, the last one carrying the summary — instead of one object at the end.

## [0.9.13] - 2026-08-15

Importing a workout archive stops taking an afternoon.

### Changed
- A bulk import renders once at the end instead of once per file. Rendering costs roughly the whole archive rather than the file just added, so a few hundred workouts were paying that price a few hundred times over — the later a file was imported, the more it cost. Files are now stamped into the archive as they arrive, the server writes down which tiles went stale, and one pass at the end settles the lot. The progress line says when it switches from importing to drawing.
- Rendering uses the cores available to it. Work is queued as one flat list of (view, tile) jobs and handed to a pool of worker processes — not one worker per view, because the cumulative view is a single job that takes longer than every year view put together, so that arrangement leaves most of the machine watching one job finish. A full re-render of a 318-event archive went from 161 s to 57 s on eight cores. Set IRFARAN_RENDER_WORKERS to override; the default leaves one core free.
- Deep zoom levels only rasterise the part of a track that could reach the tile being built. A morning run crossing ten tiles was being stamped end to end once per tile, and at z16 each pass resamples at four times the density.

### Added
- GET and POST /api/render, reporting and settling whatever a deferred import left owing. The debt lives in the database, so closing the browser mid-import loses nothing.

## [0.9.12] - 2026-08-15

Brush strokes that look like brush strokes.

### Changed
- The tile pyramid is rendered two levels deeper, to z16. Everything is still stored on the native z14 grid, where a 15 m brush is a two-pixel disc — and the client was magnifying that up to sixteen times to reach street level, which is why a hand-drawn stroke arrived as a blurred, visibly stepped smear. z15 and z16 are now stamped from the same geometry at their own resolution rather than upscaled, so a 20 m brush is a twelve-pixel disc at z16 instead of a three-pixel one. Nothing new is stored, the event log is still the only source of truth, and a rebuild is still byte-identical.
- Tracks hand over from the bitmap to their real geometry at z16 rather than z14, since up to there the bitmap is now the sharper of the two and is the only thing carrying how many times a pixel was crossed.

Rendering costs more than it did: a full re-render of a nineteen-year archive goes from about eleven seconds to about twenty-three, and a single stroke from about one and a half to about two and a half. The pyramid itself stays small — most deep tiles are empty and are never written.

## [0.9.11] - 2026-08-15

Drawing you can see, borders you can see, and fog in a colour you chose.

### Added
- Country borders, with a toggle in settings. The basemap has always drawn them underneath the fog, which at any usable fog thickness means not at all; these are drawn on top, so the shape of a country is readable across ground nobody has visited.
- A fog colour picker in settings — colour wheel and hex, set per map theme. Unlike thickness, the colour is baked into the tiles, so applying it re-renders them; that takes a few seconds and the button says so while it works.
- Plus and minus buttons either side of the brush slider, for the last metre or two that a slider makes fiddly.
- A Zoom to 14 button appears on the drawing toolbar whenever the map is too far out to draw.

### Changed
- The Off tool is now Pan, with a hand and a grab cursor. Panning was always what it did; calling it "Off" made it look like the absence of a tool rather than one of them.
- Tracks are easier to see when zoomed in. The white line now has a dark casing under it, so it reads over cleared ground as well as over fog, and the trail bitmap fades to a low glow rather than to nothing — it is the only thing carrying how many times a pixel was crossed.

### Fixed
- The stroke preview added in 0.9.10 never appeared. Attaching the trail layer immediately before it flipped MapLibre's isStyleLoaded() to false — that reports whether every source has finished loading, not whether the style is usable — and the preview's own guard on that flag then skipped it entirely, silently, every time.
- The zoom lock no longer says "Zoom to 14 or closer to draw. Currently 14.0." The reading was rounded, so being a fraction of a level short displayed as being exactly there. It is floored now, and there is a button to close the gap.

## [0.9.10] - 2026-08-15

You can see what you are about to draw, and what you just drew.

### Added
- A ring follows the cursor showing the brush footprint at true ground scale, so the question "will this cover that side street" has an answer before the stroke rather than after it. It turns red for the eraser.
- The stroke appears as you draw it, at the width it will land, instead of arriving a second or two later when the rebuilt tiles come back. The point-to-point tool rubber-bands to the cursor so the segment being aimed is visible. The preview holds until the real tiles arrive, so nothing blinks out in between.
- Brush width has a slider on the drawing toolbar, next to the drawing. The number field in settings is still there and the two follow each other.

### Fixed
- Ground that has been erased can be drawn on again. Erase is subtracted when a tile is composed, so a later stroke over erased ground went into the archive and was subtracted straight back out — it simply never appeared, with no error to explain why. A new stroke now lifts the erase from the ground it covers. Erases still survive rebuilds and re-imports untouched, which is what invariant 2 requires; what changes is only the case it does not cover, a deliberate redraw.

## [0.9.9] - 2026-08-15

Drawing that behaves like drawing.

### Changed
- The eraser is a tool now, sitting alongside Brush and Line, instead of a separate Reveal/Erase switch. A control labelled "Erase" that lives away from the tool it modifies reads as a button that erases something, which is the wrong thing for a drawing app to be ambiguous about. It turns red while it is armed.
- Tracks are drawn much thinner. Above z14 the trail bitmap was being magnified up to sixteen times, turning a one-pixel track into a wide, blurred, visibly stepped stripe. The real track geometry now fades in over it as you zoom past z14 and the bitmap fades out, so a track stays a hairline at every zoom. The bitmap itself is also drawn at the thinnest width the grid can hold.
- Freehand strokes are smoothed before they are saved. The stroke is thinned, simplified and then run through two passes of corner cutting, so the curve is smooth in the stored geometry rather than only in how it is drawn — it survives a rebuild and still looks right at z18. Point-to-point lines are left straight, which is the point of them.
- Undo says what it is doing while it does it. Deleting a stroke rebuilds tiles, which took long enough that it looked like nothing had happened.

### Fixed
- Undo works after a page reload. It used to remember only the strokes drawn since the page loaded and would claim there was nothing to undo while the stroke was plainly still on screen; it now falls back to the most recent hand-drawn stroke on the server.
- Drawing, erasing, undoing and importing now rebuild only the tiles they touched and those tiles' ancestors, as section 6 always specified, rather than re-encoding every tile of every affected view. A single stroke re-encodes sixty tiles instead of several hundred per view, and the difference is the difference between an edit landing immediately and appearing to hang.
- An erase no longer re-renders years it cannot have changed. Erasing is subtracted from every view, so every view was being rebuilt — including years nobody travelled anywhere near the erased ground, which rendered back to exactly the bytes already on disk. On a nineteen-year archive an erase took about four seconds; it now takes about one.

## [0.9.8] - 2026-08-15

A first run that hands you what you need and gets out of the way.

### Added
- Irfaran now generates its own API token on first start and shows it on the setup screen, so a fresh install works without inventing one first. Setting IRFARAN_TOKEN still overrides it.

### Changed
- The setup screen recognises a basemap that is already installed and simply offers to continue, rather than asking you to download one you have.
- Fog is a dark grey rather than near black, and starts at 80% thickness, so thinning it actually reveals the map underneath.

## [0.9.7] - 2026-08-15

The map draws.

### Fixed
- The basemap draws. Vector map data was never being fetched, so the map showed nothing underneath the fog no matter how thin the fog was made.

### Added
- The diagnostics panel now reports the running build and how much basemap data the browser has actually fetched.

## [0.9.6] - 2026-08-15

The map appears.

### Fixed
- The basemap draws. The map was being told where its tiles were but not what was inside them, so every layer referred to data MapLibre could not resolve — the source never finished loading, drew nothing, and reported no error. A blank map that looked healthy in every other respect.

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
- Pages no longer come up half empty. Opening Irfaran fires several requests at once, and under that load most of them were failing with a server error while the same request made on its own succeeded. Places, markers, data sources and the year slider could all silently fail to appear.

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
- First-run setup screen. Irfaran now fetches its own basemap - pick one of the recent Protomaps planet builds or give it a URL, and watch the progress. The download resumes if it is interrupted, and the archive is checked before it is installed.
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

[Unreleased]: https://github.com/n0ne117/Irfaran/compare/v0.8.3...HEAD
[0.8.3]: https://github.com/n0ne117/Irfaran/releases/tag/v0.8.3
[0.8.2]: https://github.com/n0ne117/Irfaran/releases/tag/v0.8.2
[0.8.1]: https://github.com/n0ne117/Irfaran/releases/tag/v0.8.1
[0.8.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.8.0
[0.6.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.6.0
[0.5.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.5.0
[0.4.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.4.0
[0.3.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.3.0
[0.2.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.2.0
[0.1.0]: https://github.com/n0ne117/Irfaran/releases/tag/v0.1.0
