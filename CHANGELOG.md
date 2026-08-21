# Changelog

All notable changes to Irfaran are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions are `MAJOR.MINOR.PATCH` — patch bumps on every code change, minor bumps on phase completion, `1.0.0` when running live on real data.

Entries are written for someone reading the release page, not for someone reading a git log. Describe what changed for the user, not which file was touched.

## [Unreleased]

Nothing yet.

## [0.17.19] - 2026-08-21

### Added
- **The basemap's own place names can be searched.** Settings, Search reads them out of the installed archive once and searches them offline afterwards — 478,382 tiles, **two and a half minutes, 1,069,426 names** on the 137 GB planet build. Ferrara, Gumpendorfer­straße, every town and village. Points of interest are the same feature at a different scale and have their own switch: they exist only in the deepest tiles, so reading them means going through the whole archive, and the button says so before it starts.
- **"This view" narrows a search to what is on screen.** It earns its place the moment basemap names are searchable: a pizzeria called Eleven is one of hundreds with that name, and the one somebody means is the one they are looking at. It narrows your own pins and tracks too.
- Reading the archive is a background job that **gives way to the render queue** rather than competing with it, so drawing and importing stay as quick as they were. It writes a new generation and only swaps at the end, so a build that fails, is stopped, or is interrupted by a restart leaves a working index rather than a hole — and resuming is a cursor, costing one batch rather than the whole scan. Each index records which archive it came from and says so when the basemap has been replaced since.

### Fixed
- **"Switched off under Settings, Appearance" had been pointing at the wrong tab since 0.17.18**, when search settings moved to a page of their own. Caught only because the message changed for an unrelated reason.
- A failed search no longer lists the basemap's names among what is "switched off". They are usually off because they have not been read out of the archive yet, which is a different thing, and mentioning them to somebody searching for a pin is noise. And three excluded kinds now read "A, B and C" rather than "A and B and C".

### Note
No new dependency. Reading a PMTiles archive is a fixed header, varint directories and a Hilbert curve; reading points out of a vector tile is varints and zigzag pairs over protobuf wire format. Both are written here, in the same spirit as the Plus Code decoder and the icons, and both are checked against things that can be verified independently — the decoder finds Wien at 48.2082, 16.3724, and a pizzeria called Eleven at 45.59841, 12.88348 in Caorle.

Two of the estimates in `IDEAS.md` were wrong, and measuring is what fixed them. Place names were guessed at 1.4 million tiles across seven cores and are a quarter of that on one, because more than half of every zoom is empty ocean stored once. And labels are buffered into neighbouring tiles, so **55% of everything a scan reads is a repeat** — dropped by remembering 400,000 recent keys, which caught 99.8% of them in the real build, with query time collapsing the remainder.

## [0.17.18] - 2026-08-20

### Changed
- **Search has its own settings page**, between Places and Backup. The five toggles moved out of Appearance, where they never belonged, and the page has room for what searching grows into. It also says plainly that searching the basemap for a place name is not built, and why.

### Note
Costed the offline gazetteer against the installed archive rather than guessing, and found the thing that changes its shape: the basemap has a **`pois` layer** as well as `places`, so a pub is reachable in principle and not only a town. They sit at different depths, which makes them two features rather than one — settlements at `min_zoom` 10 and below are **1,398,101 tiles**, minutes of work; points of interest reach to z15 and mean reading all **135,371,839 distinct blobs**, 137 GB, an estimated 2.7–10.7 hours across seven cores. Better than it sounds, because a scan reads distinct blobs and there are 135 million of those against 1.43 billion addresses — the rest is repeated empty ocean.

Also written down, before any of it is built: a replaced basemap means a full re-extraction with no useful shortcut, so a build has to happen beside the running index and swap at the end, and record which archive it came from or it silently goes stale. And the priority rule is the opposite of locking — manual work wins and the scan yields, reusing the stop-and-resume machinery the render queue already has. Progress on In progress, controls on the Search page: two views of the same work drift, which is how an import came to sit at 100% after finishing.

## [0.17.17] - 2026-08-20

### Added
- **Plus Codes, full and short, each with its own toggle and both off by default.** `8FVC9G8F+6W` decodes to a position with arithmetic — no table, no lookup, nothing leaving the machine, which is why this belongs here where a postal code would not. The decoder is checked against three independently verifiable points: Zurich, Google's own documented example at 1600 Amphitheatre Parkway (`37.4220625, -122.0840625`), and a vector from the specification. Every code round-trips through encoding exactly.
- **A short code is resolved from where the map is looking**, since it is missing its leading digits. That makes it right only within about half a degree of the map centre, and a wrong recovery cannot be detected — with the map on Sydney, a Zurich code resolves near Sydney, confidently and silently. So the result is labelled with the **recovered full code** rather than what was typed: `4RRH9G8F+6W` is visibly not Zurich to somebody who meant Zurich. The same principle as refusing to silently swap a reversed latitude and longitude.
- Two toggles rather than one, because a full code is arithmetic and a short code is a guess against context. A short code with no map position says so instead of guessing.

### Note
The two settings tests that broke on this were both pinned to snapshots rather than properties — one listed the three kinds of search by hand, the other asserted the exact wording of a hint. Both now assert what they are actually about, with anchors so they still record what shipped. A test pinned to a snapshot fails whenever the snapshot improves, which teaches you to edit tests rather than read them.

The external geocoder moved to its own **Not doing** section in `IDEAS.md`, and the offline gazetteer is now costed from the installed archive's own header rather than guessed at: 137.3 GB addressing 1,431,655,765 tiles, of which 135,371,839 are distinct, with a `min_zoom` on every place label — which is what makes a z0–z10 scan (1.4M tiles, minutes across seven cores) enough for towns and villages, against 250–400 MB of SQLite for the result.

## [0.17.16] - 2026-08-20

### Added
- **Search settings, under Settings, Appearance.** A toggle for each kind of thing the bar looks through — pins, tracks, coordinates — where unticking one removes it from the results rather than hiding it on the map. **Tracks start off**: a name search otherwise answers mostly with track segments, which is what asking for these settings was about.
- Server settings rather than browser ones, because the filtering happens where the searching happens, and a preference about your own archive belongs to the archive rather than to whichever browser you last used. They travel with an export, like the theme and the fog colour.
- **An excluded kind says so.** Searching a track name with tracks switched off answers "Nothing here matches 'miramare'. Tracks are switched off under Settings, Appearance." Otherwise something plainly visible on the map is unfindable for no stated reason.

### Note
**Coordinates default to on, which is a deliberate exception to "pins only".** Pasting a coordinate is not a search of anything stored — it reads what was typed — so defaulting it off would silently remove a working feature rather than quieten a noisy one. It is a toggle like the others for anyone who disagrees.

Absent settings read as those defaults rather than as everything switched off, so a database from before they existed behaves like a fresh one. Pinned by a test, because the alternative is an upgrade that quietly disables search.

Six tests broke on the new default, and the two fixes were different problems. Five test what searching *finds*, so their fixture now switches every kind on and the default is tested in one place. The sixth asserted the exact wording of the "nothing found" hint — wording that has now changed three times as the feature grew, each version true when written. It asserts the property instead: an empty answer explains itself. A test pinned to a sentence breaks whenever the sentence improves, which teaches you to edit tests rather than read them.

## [0.17.15] - 2026-08-20

### Added
- **The search bar suggests as you type.** Matches appear after a short pause rather than on pressing Go, with arrow keys to walk them and Enter to take one. Measured in the browser: a six-letter word costs **one request, not six**, and anything shorter than two letters is not sent at all — one letter matches half an archive and the answer arrives too late to be about what is on screen.
- **Pins are searchable by who was there.** The column was being read and then ignored, so the one field somebody fills in by hand was the one field that could not find anything. `andrea` now answers with eight pins here.

### Note
Two decisions that could have gone the wrong way. **Typing suggests, it does not travel:** typing toward `27.74367, -15.58338` passes through `27.74367, -1`, which is a real place off the coast of Africa, so flying on each keystroke would drag the map through the wrong continent on the way to the right one. And **late answers are dropped**: replies can arrive out of order, so a slow response for `cao` must not overwrite the results for `caorle`. Each request carries a sequence number, and the guard covers the failure path as well as the success one.

Both are pinned by tests that read the source, since the TypeScript has no test runner: one asserts `suggest` contains no `flyTo` or `fitBounds`, another that the stale-answer check appears on both paths.

## [0.17.14] - 2026-08-20

### Changed
- **The import log lists every file, and fills the panel.** It kept the last six outcomes, which on a seventy-file drop threw away most of the answer. It now takes whatever room is left down to the bottom of the settings sheet and scrolls past that: measured at **4 rows on a 560 px window, 13 at 800, 24 at 1100**, where it was six regardless.
- The cap existed so a long list would not grow past the panel. The panel scrolling is the better answer to that, and the log can shrink rather than pushing its own controls off screen — checked at 560 px, where "Choose files" stays visible.
- Before the first import the section stays the size of its controls rather than reserving a screenful of blank space, and only the import tab is stretched: the other tabs are cards that end where their content does.

## [0.17.13] - 2026-08-20

### Fixed
- **One panel failing to start no longer takes the rest of the page with it.** The wiring in `start()` ran in a straight line, so the first component to throw took every handler after it — and the Import button is wired forty lines below the search bar. Nothing appeared on screen and nothing in the interface suggested why: a dead button, an enabled-looking dead button, with the browser console the only clue and only if you thought to look. Each component is now wired independently, and a failure names itself in a line at the top of the map: "Some of the interface could not start: search. The rest still works."
- Verified by breaking one element id deliberately and reloading: the banner said `search`, and the Import button kept working. Guarded from Python, so a component added with a bare `.wire()` fails the suite rather than quietly restoring the cascade.

### Note
Raised as "a token is set, but the Import button does not react", on a freshly loaded page. It could not be reproduced here — handler attached and firing, button enabled and not inert, nothing overlaying it, no console errors, `index.html` served `no-cache` with immutable hashed assets and no service worker — so this release does not claim to fix that report. What it fixes is the reason the report had no diagnosis: the failure was silent, and a silent cascade is worth removing whether or not it was the cause.

## [0.17.12] - 2026-08-20

### Added
- **Search your own pins and tracks.** Pins by title, tag, label, folder or category; tracks by name or by year. Results are a list with the reason each one matched — `Caorle` answers with the pin, then `Dörfl -> Caorle` and `Caorle -> Dörfl` — and picking one flies to a pin or frames a whole track. Arrow keys walk the list, Enter takes the highlighted one.
- **Searching is read-only, so it needs no token.** Seeing where you have been is what the map already shows. Keeping a searched coordinate as a pin is the write, so that offer appears only when there is a token to make it with — the coordinate is still found, flown to and marked without one, with a line saying where to put the token rather than a button that fails when pressed.

### Fixed
- Tracks are grouped by name. One imported file becomes one event per gap in it — a single 827 km ride is 37 of them here — so an ungrouped answer would bury everything else. A track carries a bounding box and the map frames all of it, because the middle of a 400 km ride is a field.

### Note
Case folding is done in Python rather than in SQL. SQLite's `LIKE` and `lower()` fold ASCII only, so `dörfl` would never have found `Dörfl`, and in this archive most names carry an umlaut.

**A track can only be searched by year, and the first version of this got it wrong.** It matched months against `created_at`, which is `datetime.now()` at ingest for every source — so it would have answered "you were there in August 2026" about a ride imported that month and taken years earlier. The activity's own date survives only as the year in `layers`. `2024-06` now explains that instead of guessing, while `2024-12` still matches the live tracks that are *named* after a timestamp.

Geometry is read only for the results being returned. The first version computed every candidate track's bounding box while matching, which measured **27.8 MB a search** on this archive — cost set by how much has been walked rather than by what was asked for, which is the shape of the scan that made every render slow before 0.17.6. Names first, geometry for the twenty that survive: 18–61 ms.

## [0.17.11] - 2026-08-20

### Fixed
- **The progress bar above the time bar could be left stuck part way.** Reported after a run of reveal strokes: the fog was cleared, every point was drawn, and the bar sat at about three quarters indefinitely. Only the readout was wrong — the strokes, the fog and the render had all finished.
- `show()` sets a timer that hides a good-news message after four seconds; `progress()` cancels that timer, deliberately, because a bar that vanishes mid-render is worse than one that sits there. So whoever paints progress owns putting the notice back — and the watcher callback ignores the final poll on purpose, since there is no progress to report once a render is done. Nothing was left to clear the bar, so it stayed wherever the last running poll had found it. Drawing and undo now both end on their summary line, which clears itself; if the watcher loses track instead, it says so and points at Settings, In progress, rather than leaving a stuck bar by another route.

### Note
Guarded from Python, the way `test_markup.py` guards element ids, because there is no test runner for the TypeScript: removing the fix fails two tests in `test_progress_notice.py`. It also pins the two `notice()` behaviours the fix depends on, so if `progress()` ever stops cancelling that timer the reason this code exists stays visible.

The mechanism was reproduced rather than assumed, by running `notice()`'s own timer logic on a scratch element: without the fix the notice was still visible long after the timer should have hidden it, reading `74/100` and "Drawing…"; with it, hidden.

## [0.17.10] - 2026-08-20

### Added
- **Search, starting with coordinates.** The magnifying glass beside the settings button opens a bar across the map. Paste `27.74367, -15.58338` and the map flies there. It takes what a paste actually looks like: comma or space, degree signs, hemisphere letters on either side of either number, and degrees-minutes-seconds — `27°44'37.2"N 15°35'00.2"W`, typographic quote marks included. All the forms of one point land on that point.
- **A found coordinate drops a temporary pin**, dashed and hollow so it never looks like a pin that has been saved, offering a name and a choice: keep it or discard it. Until it is kept it costs nothing — no event, no render, no row. Keeping it goes through the same path as dropping a pin by hand, so it clears fog and reaches the sidebar identically.
- **A pair written the other way round is named, not swapped.** `120.5, 45.2` answers "out of range as latitude, longitude. Written the other way round it is 45.2, 120.5." Being taken confidently to the wrong continent is worse than being told the input was not understood — so nothing is guessed at, and `Vienna` says plainly that searching your own pins and tracks is not built yet.

### Note
Parsing lives on the server rather than in the browser: one implementation, tested beside everything else, and `GET /api/search` already has the shape the rest of search needs — a list of results, each with somewhere to go. Nothing about a search leaves the machine, which is the whole reason an external geocoder was argued against.

The flight itself is the one part that could not be verified here: the browser pane produces no animation frames, so the camera cannot move and the zoom readout stays where it was. The marker's coordinates were checked instead. The flight is capped at 1.4 s because the default duration scales with distance, and a world view to a street is the longest journey the map can make.

## [0.17.9] - 2026-08-19

### Changed
- **Country borders are dark grey instead of amber.** Amber is the trail ramp's own colour, so a border crossing a route read as part of the route. Measured against the warm end of that ramp the old line scored a contrast ratio of **1.14** — near enough the same colour — where the new one scores 6.21.
- Two greys, because the two themes are not the same problem. On light the change is pure gain: against the fog as actually painted the old amber scored **1.78**, effectively invisible, and `#2b2b31` scores **10.84**. On dark it costs something: amber scored 3.19 there and `#101014` scores 2.58. That is the one thing given up, and it is worth giving up to stop borders reading as tracks. Darker was chosen deliberately over lighter — borders earn their keep over ground nobody has visited, which is fog, and against fog a darker line reads better.

### Note
Verified numerically and by checking the built bundle, not by eye: the browser pane would not composite frames, so no screenshot was possible. The contrast figures use the fog as it is actually drawn — 80% opacity over the basemap, so `#56555c` on dark and `#e2e2df` on light — rather than the fog colour on its own.

## [0.17.8] - 2026-08-19

### Fixed
- **Editing a pin on a restored archive answered 500.** Reported from a fresh install: changing a pin's label failed there while the same edit worked on the instance the data came from. A pin's fog is an event and `places.event_id` links them, but the import inserted pins without that link — so every restored pin looked like a pin whose fog had never been stamped, which is the one condition under which editing a pin re-stamps it. That insert carries `external_id = "place-<id>"`, the archive had already brought an event with exactly that pair, and a UNIQUE index covers `(source, external_id)`: `IntegrityError: UNIQUE constraint failed: events.source, events.external_id`. A label change was simply the first edit tried — renaming, retagging or moving would all have done it, on every restored pin.
- **The import now restores the link**, finding each pin's event by the `external_id` it was exported under and renaming it to match its new id where that name is free. Fixes future restores.
- **Stamping now replaces a leftover event rather than colliding with it**, returning its tiles for rebuilding so the fog it drew does not outlive it. This is what heals an instance that has already been restored by an earlier version — fixing the import does nothing for a database sitting in that state today. The first edit of each such pin re-stamps its fog, correctly, at the cost of one small render per pin.

### Note
Both halves are pinned by tests that fail without them, covering different machines: removing the stamping guard reproduces the reported `IntegrityError` on an already-restored database; removing the import link leaves restored pins orphaned. The second case is built by blanking `event_id` on a restored archive, because that is the state the reporting instance was actually in and no amount of fixing the importer would have reached it.

## [0.17.7] - 2026-08-19

### Fixed
- **An import could sit at 100% and never say it had finished.** Reported on a fresh install restoring a full archive: the render completed — 276,752 tiles across 10 views, recorded in the history — while the screen watching it stayed on "Drawing the map — 100%" indefinitely. Two independent faults, which is why it took a large restore to show. Nothing in the browser set a request timeout, so one unanswered status poll stalled the watcher for good; and a *failed* poll made the watcher hand back the last state it had seen, which would have reported an unfinished render as done. It could lie in either direction.
- Status polls now have a 15-second deadline, and five consecutive misses are ridden out before the watcher admits it lost track — at which point the import screen says the render is still going on the server and points at Settings, In progress, rather than claiming to be finished.
- **The status endpoint could take seconds to answer while a render was running.** It worked out the whole job count whenever the number of owed tiles changed, which is every time a pass completes: on a large archive that means asking every view which of thousands of owed tiles it holds, and on the reported machine a basemap download was competing for the same disk. While a render is going it now answers from what it already knows — the panel is reading the worker's own counters at that point anyway, and a stale figure costs nothing next to a poll that never returns.
- The remaining-time line no longer reads "100%" in the brief window between passes where the counters agree while the next pass is being worked out. A bar at 100% beside the word "drawing" is what a hang looks like even when nothing is wrong.

### Note
The first two tests written for the endpoint half passed with the fix removed: they never created a pass boundary, and the boundary is the only moment the expensive recompute happens. Rewritten to defer work mid-render, they fail with "the poll worked out the job count 2 times during a render" — one by counting the calls, one by making the count deliberately slow and timing the poll.

## [0.17.6] - 2026-08-19

### Fixed
- **Every render was scanning the whole blob store, once per tile.** Compositing asks for one tile at a time — kind, x, y — and the `blobs` primary key is `(kind, source, layer, x, y)`, so only `kind` was a usable prefix and the rest was a scan. Because the table is `WITHOUT ROWID` the rows being scanned carry their blobs with them, which made reading one tile's fog a walk over every fog blob in the archive: **226 MB, three times per tile**. The pyramid walk does that for every native tile in a view, so the cost was the tile count multiplied by the table size — quadratic in the archive, which is why the same two views went from 142 to 324 seconds over one morning of importing.
- A 228 KB index on `blobs(x, y)`, built in 0.02 s, turns each lookup from a scan into a seek. Measured on a real 2,954-tile view, one whole-view walk went from **247.5 s to 8.8 s**; a stroke-sized render across the two views a hand-drawn stroke actually touches went from about **257 s to 11 s** on the same archive. That walk was the entire reason a stroke took five minutes to appear and the progress bar sat at 98% while it happened. Existing archives gain the index on the next restart.
- **The progress bar restarted at 0% partway through a render.** A run takes as many passes as the work needs — the loop that stops mid-render arrivals being lost — but each pass counted from zero, so two strokes in quick succession sent the bar to 100%, back to nothing, and up again. Done, total and tiles written now accumulate across the whole run.

### Note
The wide-zoom walk was split into one job per z10 subtree first, on the theory that the walk was inherently expensive and needed more cores. It was built, tested byte-for-byte against the single-pass walk, and measured: **1.38×**, at 2.5× the total CPU. With the index it was worth 1.2% — 9 s against 7.7 s. It was reverted. The diagnosis had been wrong, and the evidence was in the profile the whole time: 22 ms per SQLite `execute` was read as the cost of compositing rather than as the wrong question being asked of the database.

Tests pin the query plan rather than a stopwatch — a timer in CI measures the CI machine, while the plan is the thing that was actually wrong. One asserts the index is used; one asserts the old `SEARCH blobs USING PRIMARY KEY (kind=?)` never comes back.

## [0.17.5] - 2026-08-19

### Changed
- **Drawing on the map now renders through the server queue**, so a stroke appears in the **In progress** panel alongside imports and everything else. Drawing used to render inside the request that saved it, streaming its progress back — which meant the browser was carrying the work, and nothing outside that one notice knew a render was happening. The stroke is stored and the queue is started before the response comes back; the panel shows it, and closing the tab no longer stops it. Undo goes the same way, for the same reason: putting the fog back is the same size of render as drawing it was.
- **A stroke says which views it belongs to.** Without that, the queue works it out from the tiles, and the answer is wider than the truth — a stroke drawn into 2024 changes 2024 and the cumulative view, while the tile underneath may hold a dozen other years it did not touch. Each extra view costs a full pass over z0–z13. On this archive 93% of tiles hold two views and the busiest holds twelve, and the busiest tile is the one you have walked most, which is exactly where you draw. Measured before it was written, because deferring blindly would have made drawing at home about six times more expensive than the inline render it replaced.

### Fixed
- **Work deferred while a render was running was marked finished without being drawn.** A pass reads the tiles owing once, at the start, and builds its job list from that — then cleared the whole table on the way out, including rows added while it worked. So a stroke drawn during a render, a phone reporting a fix, or a pin dropped mid-pass left tiles stale with nothing left to say they owed anything: the debt was settled and the ground was never drawn. A pass now clears exactly the tiles it took, and looks again before it finishes, so late arrivals get a second pass instead of a silent write-off. Reachable before this release through live tracking and pin drops; unavoidable after it, since drawing while a render runs stopped being unusual and became normal.
- **The drawing toolbar changed height as you used it.** The hint line was a full-width item inside the bar and the controls wrapped when the "Zoom to 14" button appeared, so the box moved between one line and two — under the pointer, while drawing. The hint now sits beneath the bar as its own line and the toolbar is always exactly one: when space runs short the brush slider gives up width and the tools keep theirs.

### Note
The test written for the deferred-work bug initially asserted that late tiles were *still owing* after the pass. That was the shape of the fix in isolation, not of the behaviour anybody wants — with the second pass in place the queue draws them instead, and the test passed for the wrong reason. It now checks the tiles are on disk, and fails with "tiles deferred while a render ran were cleared without being drawn" when the fix is removed.

## [0.17.4] - 2026-08-19

### Fixed
- **A render reply could contradict itself**, saying `state: running` and `can_stop: false` at the same time — which is not something a panel can render sensibly, and which stopped 0.17.2 and 0.17.3 from ever publishing. `state` came from a string in memory while `can_stop` came from whether the worker thread was alive, and those disagree at the edges: starting sets the state before the thread begins, and the thread exits before the state settles. Both are now derived from the state being reported, so a reply always agrees with itself.

Only reproducible on a machine with few cores, where those windows are wide enough to land in — found by running the suite under `--cpus=2` to match the CI runner, having watched two releases fail on a suite that was green on eight cores.

## [0.17.3] - 2026-08-19

### Fixed
- **A render of more than about a thousand tiles failed outright.** `views_touching` asks which views hold data in a set of tiles by naming every one of them in a single `(x = ? AND y = ?) OR …`, and SQLite refuses an expression tree deeper than a thousand. 559 tiles worked; 1,646 answered `500 Expression tree is too large`. Nothing warns in between — the query simply stops working once an archive is big enough, so a large enough import would have hit it unprompted. Asked in batches of 400 now, with a test at 1,500 tiles.
- **The status endpoint was too slow to poll.** It worked out the whole job count on every call: for each of eighteen views, which of the owed tiles it holds, then each of those expanded to its descendants. At 1,646 tiles it took longer than the request would wait. The set of owed tiles cannot change during a pass, so the count is now worked out once and cached against how many tiles owe it — a poll went from timing out to **5 ms**. Yesterday's fix covered the live half of that endpoint and left the same mistake in the owed half.
- **`irfaran.cli render` used one core.** It looped the single-view render rather than the job queue, and measured about three times slower than the server's own path on the same archive. It now uses the same parallel path — 677% CPU across ten processes where it had been sitting at 99%.

## [0.17.2] - 2026-08-19

### Fixed
- **A resumed render deleted the work it had skipped.** Pruning removes tiles inside the scope that a pass did not write — that is how ground whose data has gone stops being drawn. On a resumed pass the jobs finished before the interruption are handed in as work to skip, so their tiles were absent from that accounting and pruning deleted them: the resume destroyed exactly what it existed to preserve. Measured on a real archive: about 1,300 deep tiles gone from the cumulative view, which is rendered first and so was almost entirely skipped. The symptom was tracks vanishing when zoomed in, and the giveaway was `all` holding fewer tiles than a single year view — impossible, since it is the union of all of them.
- A resumed pass now leaves stale tiles alone, and they are removed by the next pass that runs start to finish. That is the cheaper mistake by a wide margin: a lingering tile shows ground you no longer have data for, a deleted one shows nothing where you do.

Introduced yesterday in 0.17.0 along with resumable renders, so it only ever affected a render that was interrupted and resumed.

### Note
The first test written for this passed whether the bug was present or not: it skipped *every* job, which leaves the accounting empty, and pruning is skipped when the accounting is empty. The bug needs one job to run. The test now skips all but one and fails with "a resume deleted 38 tiles belonging to jobs it skipped" when the guard is removed.

## [0.17.1] - 2026-08-19

### Fixed
- Pressing Resume or Stop put a `TypeError: can't access property "toLocaleString"` on the page. Those two endpoints replied with the worker's snapshot alone — which knows what is being drawn but nothing about what is owed — so the panel painted a reply where `pending_tiles`, `jobs` and their neighbours were simply absent, and formatting an absent number threw. All three render endpoints answer with one shape now, built in one place, and the panel reads every number through a default so a missing field can never take the page down again.

There is a test asserting each of the five possible replies — status, a start, a refused start, a stop and a refused stop — carries every field the interface reads, and that no number it formats is ever null.

### Note
Also removed a duplicate `/api/render/stop` route left behind while rebuilding these endpoints. The new one was registered first so it was the one being served, but two definitions of the same route is a trap waiting for whoever edits it next.

## [0.17.0] - 2026-08-18

### Changed
- **Rendering is the server's job now, and no browser is part of it.** It used to be driven by a request that streamed its progress, which meant the work only advanced while something held that response open — so closing a tab stopped a render mid-pyramid and left the map half drawn, with nothing in the interface offering a way back. There is one queue in the API process, one render at a time, and a state anyone can read. Start it and close the browser: it carries on. Come back and it tells you where it got to.
- Progress is polled rather than streamed, and the status is answered from the worker's own memory without touching a table. The old status endpoint recomputed the job count from the database on every call, which under a running render meant competing with it for the same locks — it timed out under exactly the load it existed to describe.

### Added
- **An In progress tab** under Settings: what is being drawn, how far along, how much is owed, which views are affected, and buttons to resume or stop. It polls only while it is on screen.
- **Renders are resumable.** Each finished job is written down, so a pass interrupted by a button, a closed browser, a restart or a power cut carries on rather than starting again — and can say exactly how much is left. The job in flight when the lights go out is the only work ever repeated.
- **Stop after this tile.** Nothing already drawn is discarded and nothing owed is forgotten: stopping and resuming is a pause, not a cancel.
- The import panel now says the render carries on if you close the browser, because it does.

### Note
Two tables hold the truth between them: `pending_render` for which tiles owe a render, and `render_done` for which jobs of the current pass are finished. Both are cleared together when a pass completes.

While rebuilding the endpoints I destroyed four unrelated routes — history and all three tracker routes sat inside the block I replaced. The test suite caught it as an import error and four failing endpoint tests, and they were recovered from git. Worth recording as the reason the endpoint tests exist.

## [0.16.0] - 2026-08-18

### Added
- **Major and minor pins.** They differ in exactly two ways: a minor pin is drawn smaller, and it stops being drawn once the map is further out than z7 — where a valley with three pins in it is one smudge rather than three facts. Everything else about them is identical. Chosen on the pin itself, next to the label. Every pin that already existed is major, and changing prominence costs nothing: it is a viewing choice, so it draws no tiles.
- **Pins shrink as the map zooms out**, to a floor rather than to nothing: full size at z9 and closer, 0.8 at z7, down to 0.55 by z2. A marker is otherwise a fixed number of screen pixels at every zoom, which is right on a street and wrong on a continent. Measured 41 px at z14 down to 22.5 px at z2, with the tip staying on the same spot on the ground.

The scale is one custom property written on the map container per zoom change and inherited by every marker, and the transform goes on the marker's child — MapLibre owns the marker's own transform and uses it for positioning. Minor pins are hidden by a container attribute rather than by removing markers, so zooming back in costs nothing.

There is deliberately no transition on the scale. Zoom is already a continuous gesture, so easing on top of it adds lag — and a transition needs animation frames, which a backgrounded tab does not get, which would leave pins stuck at whatever size they were when the tab lost focus.

## [0.15.2] - 2026-08-18

### Fixed
- **Editing a pin's details no longer renders anything.** A title, a label, a tag or who was there changes no pixel, and the code that decides what to re-stamp already knew that — but the endpoint rendered regardless, and passed the empty scope on as `dirty or None`. An empty set becomes `None`, `None` means *no scope*, and no scope means render these views in full. So correcting a spelling cost a complete re-render of the cumulative view and every year the pin belonged to. Measured on a real archive: **0.07 s now, 85 s before**. Moving a pin still renders, because that genuinely moves fog.
- **The pin popup is readable in dark mode.** MapLibre ships its popups white with dark text; the content inside is ours and inherits our foreground colour, so in dark mode it was light text on a white card and the Edit button was white on white. Contrast is now 14:1 in dark and 17:1 in light. Every rule had to be qualified with `.maplibregl-popup`, because maplibre-gl.css is bundled after ours and wins at equal specificity.
- **The edit form is no longer 130 pixels wide.** MapLibre sizes a popup to its content and a column of full-width inputs has no intrinsic width, so the whole form collapsed. It has a floor now: 268 px wide with 240 px fields.
- The Who? checkboxes read as one block rather than loose rows: their own panel, hover states, and the accent colour on the boxes.

## [0.15.1] - 2026-08-18

### Added
- An import now says how big the render will be **before** it starts: "4 of 4 imported. Drawing the map — 1,198 pieces of work, about 12 minutes." Four long-distance tracks measured 12 min 43 s and 40,712 tiles on a real archive, because every z14 tile a track crosses also has its z15 and z16 descendants stamped for each theme, each kind and each view containing it — four walks round a town are twelve seconds. A wait nobody warned you about is indistinguishable from a hang, and the size is knowable before any work begins.
- The estimate is measured, not guessed. Each render records how many jobs it did and how long it took, and `GET /api/render` divides one by the other over recent renders — so the number comes from the hardware it is actually running on. Until a render has been recorded it says how much work there is and declines to guess at a duration.
- Renders in the History tab now say how long they took.

### Note
The progress bar itself was checked and is not broken: on an import of four long tracks it moved through 324 distinct steps with a settling estimate, and nginx was confirmed to stream the progress lines rather than buffer them. A render that takes twelve minutes on a bar with a thousand steps simply looks still, which is what the up-front estimate is for.

## [0.15.0] - 2026-08-18

### Added
- **Who?** on a pin. Names are registered under Settings → Places and chosen from a list, so the same person is spelled the same way on every pin and "everywhere I went with Marie" is a question that can be answered. Multiple choice, because more than one person is usually the case. Renaming somebody renames them on every pin that names them; removing somebody only stops the name being offered, and the pins that recorded them keep it.
- **A plus on every folder row**, which makes a folder inside a folder something you can find. Nesting was always supported and the only way to reach it was a parent picker that appears while dropping a pin — so making a subfolder meant dropping a pin you did not want, choosing a parent, and pressing a button elsewhere. The plus is disabled on a folder that is already as deep as folders go, and says so.

### Changed
- **A pin is edited at the pin.** Clicking one, and choosing Edit, now shows everything — title, label, who, tags, folder, coordinates — in a panel at the pin rather than in the sidebar. Somewhere on a map is a position first and a row in a list second, and a form three hundred pixels away asks you to hold the position in your head while you type. The marker stays draggable while the form is open, so the position and the title are corrected in one gesture.
- The settings tab called **Labels is now Places**, and holds both label and name registries. The sidebar keeps the pins.
- **No emoji left in the interface.** The folder eye was `👁`/`🚫` and the pan tool a raised hand, both drawn by the operating system in its own colours and its own style, which is why they could never be made to match the text beside them. Replaced with line icons drawn on a 24-unit grid and stroked in `currentColor`, so they take the colour and weight of whatever they sit in and follow the theme without being told about it. The settings cog and the draw pencil went the same way.

No icon library: a set is a dependency, a licence and a few hundred kilobytes for the seven glyphs this needs.

### Fixed
- Saving a pin did not refresh the map. Moving the form to the pin left the old sidebar picker code behind, still being called on every load and still asking for markup that no longer existed — so loading threw after the tree had painted, which looked like nothing wrong, and the redraw that follows a save never ran. The test that every id the front end asks for exists in the markup is what found it.

## [0.14.0] - 2026-08-18

### Added
- A **History** tab under Settings. Errors are red, anything done by hand is the brightest thing on the page, anything that arrived on its own is amber, and whatever the server did to itself is grey. Filter by category, and clear it if you want it gone.
- With it, the first place an error is kept at all. A failed import used to exist in the container's stdout and nowhere else: a restart discarded it and the interface could not read it, so the answer to "why is that track missing" was always "go and look at the logs on the server".
- Recorded: imports and what they found, strokes drawn and undone, live deliveries, tracker syncs, renders, settings changes, and anything that failed.

The log is capped at 2,000 entries and 90 days, because it lives in the database that gets backed up and carried between instances. A live source delivering every few minutes folds into one line that counts up rather than three hundred lines a day that push out everything worth reading. Settings changes record the *name* of what changed and never the value, since a value can be a token and history travels with a backup.

Recording is never allowed to break what it is recording — every failure writing a line is swallowed, including a locked database. A missing line in a list is a nuisance; an import that fails because a log line failed is not.

## [0.13.1] - 2026-08-18

### Added
- A **progress bar while a stroke is being drawn in**, in the notice above the time bar. A stroke is rasterised into every view it belongs to, which on a full archive measured 5.7 seconds — and the only sign of life was the preview refusing to disappear. `POST /api/events?progress=1` reports each finished unit of rendering and then the result; the plain response is unchanged, because the places page and everything else built on it expect one JSON object.
- **Errors above the time bar can be dismissed.** They used to sit there until something else replaced them, which on a map means covering part of it indefinitely. Only bad news gets the button: good news clears itself after a few seconds, and something that leaves on its own does not need one.

Notices now own their markup rather than being written to directly. Every writer set `textContent`, which would wipe a button inside the element — so the button lives behind a helper that cannot be clobbered, and the progress bar shares the same slot.

## [0.13.0] - 2026-08-18

### Fixed
- **intervals.icu sync imported nothing at all**, and said so in a way that sounded like an explanation: "7 activities, 7 without GPS". There is no GPX endpoint. `/activity/{id}.gpx` answers 404 with a real key — the 401 it gives without one comes from the security filter rather than from a route that exists, and that 401 is what 0.12.0 was built on. Activities are read from `/activity/{id}/streams` now, where positions arrive as two parallel arrays (`data` holds latitudes, `data2` longitudes) with a `time` stream of offsets in seconds hung on the activity's start date. Verified against a real account: five of seven activities imported, the two skipped genuinely have no positions, and running it again imports nothing.
- An activity with no positions is skipped without downloading anything, because the listing already says which streams exist. A winter of indoor sessions costs one request rather than one per session, and "without GPS" now means it.
- Samples where a position is null are left out rather than drawn as a line through nowhere.

### Added
- **Sync reports as it goes.** Newline-delimited JSON, the same as the render: listing, then one step per activity with a running count of what is new, then the summary. Downloading a month of activities is a minute or more, and a minute with no bytes sent is indistinguishable from a hang — to a person watching a spinner, and to a reverse proxy deciding a request has stalled.
- The render that follows a sync reports separately, so a sync that finds five rides shows the fetching and then the drawing rather than one long wait.

### Note
The tests now block outbound requests by default, so a test that reaches the real service fails loudly instead of quietly using somebody's account. The stub also speaks the real stream shape — parallel arrays and second offsets — because reading it as a list of pairs is exactly what shipped before and the old stub agreed with the bug.

## [0.12.4] - 2026-08-18

### Fixed
- A token copied out of a web page could be refused while looking character for character correct. JavaScript's `\s` covers space, tab, newline, non-breaking space and even a byte order mark, but not the zero-width family — `U+200B` and its neighbours survive trimming and splitting, and then fail a byte comparison invisibly. Those are now stripped, along with soft hyphens and control characters, on both the paste path and Apply.

### Changed
- A refused token now says **which server** refused it and **how much** was sent: "http://tower:8080 refused that token (sent 66 characters, including 2 that are not a hex digit)". "The server refused that token" is true and names none of the several possible reasons. Which server matters most — a token belongs to one instance, and a browser pointed somewhere other than you assumed is indistinguishable from a bad token — and the length catches anything that came along with the paste. Neither reveals the secret.

## [0.12.3] - 2026-08-18

### Added
- An **Apply** button beside the API token under Settings → Security, which fixes a real way of being locked out. The token used to be stored only when the field raised an `input` event — which typing does and a password manager, an autofill or any browser extension does not. So the field visibly held the correct token, nothing was stored, the status line said "No token set", and every write was refused while the answer sat on screen. Apply reads the field whatever put it there.
- Apply also verifies the token against the server before believing it, and says which happened: accepted and kept, refused by the server, or — the case that looks identical to a wrong token — a browser that will not let the page remember anything, which is what private browsing does. A refused token restores the one that was working rather than leaving the browser locked out.

Suggested by exactly the person who got locked out by it, which is the useful kind of bug report.

## [0.12.2] - 2026-08-18

### Fixed
- The unraid guide told you to read the token with `docker exec irfaran-api`, which stopped being a real container name in 0.11.6. That name came from the separate unraid Compose file, which set it explicitly; reverting to `docker-compose.prod.yml` means Compose names containers itself, as `<stack>-api-1`. The guide now says `irfaran-api-1`, explains the rule, and shows how to list them when a stack is called something else. This was the one command someone needs to recover a token, so it failing was worse than most.

### Changed
- A rejected token now says why it is likely rejected. "The header does not match the token configured on this server" is accurate and reads like a malformed token, when much the commonest cause is a perfectly good token belonging to a *different* instance — every server generates its own. Both the server's message and the setup screen's now say so, and the README and the site say it too.

## [0.12.1] - 2026-08-18

### Fixed
- Pasting the token into a new browser could be rejected as the wrong token when it was in fact the right one. `irfaran.cli token` printed the token and then a line saying where it came from, both on stdout, and the obvious thing to do with two lines of console output is to select both — after which the token really is wrong, but only because something else came with it. The token now goes to stdout alone with its provenance on stderr, so a pipe or a `$(...)` gets the token and nothing else, and both token fields take the first line of a multi-line paste.

Recovering that after the fact is not possible, which is worth writing down: a text input strips CR and LF, so by the time anything reads its value the two lines have been welded together with no whitespace between them. The first attempt at this fix split on whitespace and did nothing at all. The paste event still holds the original text, which is the only place the line break survives.

### Added
- `IDEAS.md`, cataloguing what is wanted, half-answered or deliberately postponed — trail colouring by zoom, search, snapping drawn lines to real paths, routing engines, and what was ruled out along the way and why. Deliberately not a roadmap.

## [0.12.0] - 2026-08-18

### Added
- **Workout trackers**, a second kind of data source alongside live tracking, and **intervals.icu** as the first one. Nothing is pushed: Irfaran asks, either on a timer you set in hours or when you press Sync now. Under Settings → Data sources → Workout trackers.
- Activities arrive as GPX and are filed under the same `workout` source a file drop uses, so anything already imported by hand is recognised and skipped rather than drawn a second time. Sessions with no GPS — indoor rides, pool swims — are counted and skipped rather than failing the sync, and one unreadable file does not stop the rest.
- The intervals.icu API key is stored on the server and never returned to any browser, so the field is blank every time the page opens and leaving it blank keeps the key already saved. It sits in `SECRET_SETTINGS` with the app's own token, which means no endpoint will hand it out.
- A sync defers its render the same way a bulk import does, so twenty new activities cost one render rather than twenty. A sync that ran on a timer draws them itself, because there is nobody holding a progress bar to do it.

### Changed
- The old workout-API sync idea in the build plan is dropped in favour of this. A service that already holds your history and has an API is a better place to ask than a per-vendor integration.

### Note
The live handshake with intervals.icu is the one part that cannot be tested here — that needs a real key against the real service. Everything Irfaran is responsible for is covered: the auth format the service documents, the required `oldest` parameter, dedup against hand-imported workouts, activities without GPS, unreadable files, and the key never leaving the server.

## [0.11.8] - 2026-08-18

### Changed
- New defaults, chosen so a fresh install looks right without touching a slider. Fog thickness 80%, trail colouring 50%, country borders on, individual track lines off, and the dark theme's fog is now a neutral mid grey (`#5e5c64`) instead of near-black. Track lines are off because the colouring underneath already says how often you went somewhere, and says it without turning a busy junction white. These are browser preferences, so an existing browser keeps whatever it has.
- The API token has its own **Security** tab rather than sitting above the live-tracking switches. It is not a data source, and Data sources is where the switches that need it live.
- The fog re-render warning says minutes, not seconds, and mentions that settings stay locked until it finishes. On a full archive it is minutes.

### Fixed
- The Add-a-label row no longer runs off the edge of the panel. Measured in a 300px-wide panel, the hex field ended 24px past the edge and the Add button 72px past it, with the colour swatch crushed from 38px to 8px. A flex item will not shrink below the width of its own content unless told it may, so the name field held the row wider than the panel. It can give way now, and wraps to a second line when even that is not enough.

### Added
- A test that the fog colour default in the front end matches the server's. It is written down in both places, because the settings endpoint returns what is stored rather than what is defaulted — and the two drifted the very first time the default changed, which would have shown one colour in the picker and rendered another into the tiles.

### Note on upgrading
The dark fog colour is baked into tiles, so an instance that never set one explicitly has tiles rendered in the old colour and placeholders in the new one, which shows as a seam at the edge of explored ground. Settings, Appearance, Colour of the unknown, Apply re-renders them. An instance with a colour already chosen is unaffected.

## [0.11.7] - 2026-08-18

### Changed
- The screen a second browser sees on an already-set-up server asks one question and now offers one answer to it. It used to carry three separate ways past that question — "Continue to the map" above the card, "Just look at the map" inside it, and "Continue without a basemap" underneath — for a screen whose entire purpose is to hand over a token. The card's own button is the one that stays, because its wording is the only one of the three that says what it actually does. A genuine first run is untouched and keeps both of the others.

## [0.11.6] - 2026-08-17

### Changed
- Unraid uses the same `docker-compose.prod.yml` as everywhere else again. The plugin does have somewhere to put a `.env`, so the separate variable-free compose file added in 0.11.1 was solving a problem that did not exist, and has been removed.
- The guide no longer tells you to put the basemap on the array. That advice was optimising for the wrong thing. Serving the map is not streaming, it is seeking — the browser asks for a few kilobytes at a time, roughly one request per tile, a few dozen per screenful — so seek time is the entire cost. An SSD answers in well under a millisecond, a spinning disk in about ten, and a disk unraid has spun down takes seconds before it answers at all. If the planet will not fit on fast storage, the guide now shows how to take a regional extract with `pmtiles extract` instead, which pulls one country straight out of the hosted build without downloading the rest.

### Fixed
- Fog and trail tiles are no longer re-sent to a browser that already has them. Both validators were always sent and nothing ever checked them coming back, so once the five-minute cache expired every tile on screen was downloaded again in full. They are now answered with a `304` when unchanged.
- Unexplored ground was the worse half of that, and the half that matters: a tile with no data behind it carried no validator at all, so it could never be revalidated — and on a fog-of-war map most of any screenful is unexplored, fifty-odd placeholder tiles for every rendered one. They are now tagged by their own content, which also means the tag changes by itself when the fog colour does.
- The fog appearing slowly was mostly not the fetch. MapLibre cross-fades raster tiles over 300 ms by default, so the tiles had arrived and were still fading. Fog is a flat wash of a single colour and has nothing for a cross-fade to smooth over, so it is now drawn as soon as it lands.

### Added
- Tests for all of it: conditional requests, placeholder tags per theme and per kind, and that a re-render or a recolour stops the old tag matching.

## [0.11.5] - 2026-08-17

### Fixed
- The basemap download works on a fresh install. Every button that starts one — Download, Continue in the background, Resume, and Update basemap — threw before sending anything, because the handler began by reading a token field that was deleted from the page eleven releases ago when the server started generating the token itself. Nothing reached the server, so there was nothing in the logs either. "Continue without a map" was unaffected, which is why it was the only button that appeared to work.
- The Copy button next to the generated token copies it. `navigator.clipboard` only exists in a secure context, and a self-hosted instance reached at `http://tower:8080` is not one, so on the setup this project is built for the button silently did nothing at all. It now falls back to the older clipboard call, and if even that is refused it selects the token and says to press Ctrl+C. The token is shown once, so failing quietly there was the worst possible place for it.
- The same silent failure in Settings → Diagnostics, where Copy is how a report gets out of the browser in the first place.

### Added
- A test that every id the front end asks for exists in the markup, and that no id is used twice. Both bugs above were invisible to the type checker and to any instance where the relevant panel never appears — this one is not.

The download bug survived so long because it only shows on a machine with no basemap yet. Every instance here already had one, so the buttons were never on screen to click.

## [0.11.4] - 2026-08-17

### Changed
- Tracks are visible when zoomed out. Folding the pyramid upwards keeps a track one pixel wide however far out you go, so at z7 a whole archive was 454 lit pixels scattered across nine tiles — every one at full brightness, and collectively invisible. They are thickened as the tile is drawn now, from z11 outwards: z7 went from 454 lit pixels to 2,264, and coverage across z7 to z10 is in line with the closer zooms instead of a fifth of it. Brightness was never the problem, so nothing about the colouring changed, and z12 inwards is untouched.
- The first-run screen no longer mentions the basemap when one is already installed. It used to report the size, say everything was ready, and offer to fetch a different one — a 137 GB download is not something to put a button for on the screen somebody sees before they have looked at their map even once. Settings → Basemap still has all of it. An instance with no basemap is still offered one, or a fresh install would have no way in.

The thickening is applied when a tile is drawn and never folded into the level above, or each level would thicken what the last one already had.

## [0.11.3] - 2026-08-16

### Fixed
- Overland reported "server did not acknowledge the data was received" on batches that had in fact been received perfectly. It decides that from the response *body*, not the status code: without `{"result": "ok"}` in it, a 200 means nothing. So the points arrived, the track drew on the map, and the phone kept the batch queued and sent it again — indefinitely, on battery, over mobile data. The body now says so, with the usual summary alongside it.

Nothing was duplicated by all that retrying: live points are deduplicated on their timestamp, so a replayed batch is counted and discarded. A batch refused for any real reason still does not claim to be ok.

## [0.11.2] - 2026-08-16

### Fixed
- A tracker delivering a large buffered batch could make the next write fail with a 500. SQLite allows one writer at a time, and rasterising a few hundred fixes holds it for longer than the five seconds Python waits by default — so anything arriving behind it lost. Writes now wait thirty seconds, which is longer than any write here takes.
- A write that loses anyway answers **503 with a `Retry-After`** instead of 500. The distinction matters for trackers: 500 says the payload is bad, and a tracker that believes that may drop points nobody can recover. 503 says the request was fine and the server was busy, which is the truth.

Set `IRFARAN_BUSY_TIMEOUT_S` to change the wait. Nothing is ever half-written either way — a contended write rolls back whole, which is what made this a nuisance rather than a data loss.

## [0.11.1] - 2026-08-16

### Added
- `docker-compose.unraid.yml`, with no `${VARIABLE}` in it anywhere. The unraid guide told people to edit a `.env`, and the Docker Compose Manager plugin's stack editor may not give you one — which makes every path, port and version in the normal Compose file unreachable. Everything is written out in full instead, with four marked lines to change, so the file works with nothing beside it.

### Changed
- The unraid guide creates the shares before starting rather than after, since the basemap share needs *Use cache* set to No before 137 GB starts arriving on the cache pool.

## [0.11.0] - 2026-08-16

Take your archive somewhere else.

### Added
- **Export and import**, under Settings → Backup, and offered on the setup screen of an instance that has nothing in it yet.
- An export is one file holding only what cannot be derived: the event log, pins, labels, folders, and how the map is set to look. No blobs, no tiles — those are rebuilt from the events on arrival, byte for byte, which is what invariant 1 has always promised. No basemap either: it is 137 GB of public map data the other instance can fetch for itself. A 1,244-event archive came to 5 MB.
- Import merges rather than replaces. Nothing is deleted, nothing is overwritten, and importing the same file twice changes nothing the second time — tracks already carrying a dedup key keep it, and hand-drawn events get one derived from the export they arrived in. Afterwards the map redraws with a progress readout, the same as a bulk import.

### Fixed
- **`/api/settings` and `/api/meta` were also serving the API token.** 0.10.5 stopped `/api/setup` handing it out; the token lives in the settings table, and both of those endpoints return the settings table without asking for anything. It is now filtered at the source, so the next endpoint to read settings cannot leak it by forgetting. `tokens.py` reads the row directly, which is the one place that should.

Three things deliberately stay behind in an export: the API token, which belongs to the server rather than the archive; setup state, because the new instance has its own first run; and pending render work, which is about tiles that are not travelling.

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
