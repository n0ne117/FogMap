# Irfaran

A self-hosted fog-of-war map of everywhere you've been.

Feed it GPS tracks — live from Overland, OwnTracks or Home Assistant, imported from workout files, or drawn by hand for the years before GPS existed — and it renders a persistent map where the places you've visited are revealed and everywhere else stays under fog. Inspired by [Fog of World](https://fogofworld.app/), but self-hosted, on your own hardware, with your data staying on it.

**Status:** early development. See [CHANGELOG.md](CHANGELOG.md) for what works right now.

## The name

*Irfaran* is Old High German: **to travel through a thing, and so to come to know it.**

It is *ir-* (through, to completion) on *faran*, to go — the same root that gives modern German *fahren* and English *fare*, *ferry* and *wayfarer*. What it became is *erfahren*, to experience, and *Erfahrung*, experience itself: in German, what you know is quite literally what you have travelled through.

Which is the whole idea. A map that stays under fog until you have been there does not record where you went — it records what you have come to know by going. The Anglo-Saxons had the same thought in *wīdsīþ*, the wide journey, and there is a pleasing accident in Arabic, where *ʿirfān* also means knowledge, arrived at rather than taught.

The project was called FogMap until version 0.10.0. That name was taken.

---

## How it works

The design goal is that the map stays fast no matter how much history accumulates. Everything below follows from that.

Every GPS track is rasterised once, at import, into a persistent bitmap on a web-Mercator z14 grid — 256×256 pixel tiles, about 9.5 m per pixel at the equator. Serving a tile is a file read. Nothing is rendered at request time.

The consequence is that **display cost is independent of dataset size**. A tile takes the same time to serve whether it was built from fifty points or fifty million. Zooming and panning stay fast as the archive grows, which is the entire point of the design.

Three layers, each derived from the one above it:

1. **An append-only event log** — the only source of truth. Every import and every brush stroke is an event with its own geometry, radius and time layer.
2. **z14 bitmap blobs** — derived, disposable.
3. **A pre-rendered PNG pyramid** — derived, disposable. z0 to z13 is folded up from the blobs; z15 and z16 are stamped from the same geometry at their own resolution, because a 15 m brush is two pixels at z14 and magnifying that to street level is a smear.

Delete both caches, run a rebuild, get byte-identical output. That means the whole archive can be reprocessed with different brush radii whenever you change your mind, which turns out to matter a lot when you're reconstructing places from forty years ago and your relatives keep correcting each other.

## Features

- Raster fog-of-war rendering, fast at every zoom level
- Trail layer showing route frequency, so your regular loops stand out from one-off trips
- Live tracking via Overland, OwnTracks or Home Assistant (all optional, off by default)
- Workout trackers: intervals.icu, checked on a timer or on demand (optional, off by default)
- GPX and TCX import
- Per-year subdivision maps plus a cumulative all-time view
- Manual tools: draw a route, clear fog without claiming one, enclose an area and clear all of it, or erase fog wrongly cleared by GPS drift
- Freehand and point-to-point route drawing for pre-digital history
- Pins with titles, colour-coded labels, tags, who you were with, and nested folders, each clearing the fog around it
- Independent light/dark themes for the interface and the map

## Quick start

Requires Docker and Docker Compose. Both machines this was developed against are amd64.

### Run from published images

```bash
curl -O https://raw.githubusercontent.com/n0ne117/Irfaran/main/docker-compose.prod.yml
cp .env.example .env    # edit it
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Images are published to GitHub Container Registry as `ghcr.io/n0ne117/irfaran-api` and `ghcr.io/n0ne117/irfaran-web`. Pin an explicit version tag rather than `latest`.

### Build from source

```bash
git clone https://github.com/n0ne117/Irfaran.git
cd irfaran
cp .env.example .env
docker compose build
docker compose run --rm test
docker compose up -d
docker compose exec api python -m irfaran.cli selfcheck
```

On Fedora or any SELinux-enforcing host, bind mounts need a `:z` label. The bundled compose files already have it.

On unraid, use the Docker Compose Manager plugin with the same `docker-compose.prod.yml` as everywhere else: paste it into the stack editor and set your paths under Edit Stack, Edit .env.

### Basemap

The map needs a [Protomaps](https://protomaps.com) PMTiles basemap. It is far too large to ship in a container, so Irfaran fetches it on first run: open the web interface and a setup screen offers the recent Protomaps daily planet builds, or takes a URL of your own if you'd rather use a regional extract.

The full planet is around 128 GB, which is hours of downloading. It resumes if interrupted — restarting picks up from wherever it stopped rather than starting again — and the rest of Irfaran works throughout. Only the map underneath your trails is missing until it finishes. The archive is verified before it is installed, so a URL that returns an error page is rejected rather than left sitting there looking like a basemap.

### The API token

Reading the map needs nothing. Changing anything — drawing, places, imports, the data source switches — needs a shared token.

Irfaran generates one on first start and shows it on the setup screen **once**. Keep it somewhere safe: every browser you want to edit from needs it. Once you finish setup the server stops serving it, because reads are open and an endpoint that keeps handing out the write token is a lock with the key taped to it. A browser arriving later is told the server is already set up and asked to paste the token, or to carry on read-only.

Lost it? Read it back on the server:

```bash
docker compose exec api python -m irfaran.cli token
```

The token is printed on its own line, so it is safe to pipe or wrap in `$(...)`; the line saying where it came from goes to stderr. Without a Compose project directory to run in — the unraid plugin, for instance — address the container directly, which is your stack name plus `-api-1`:

```bash
docker exec irfaran-api-1 python -m irfaran.cli token
```

**The token belongs to the server.** Every instance generates its own, so one read from one machine is refused by another — correctly, though the message sounds like a malformed token rather than somebody else's. If a token is rejected, check first that it came from the instance the browser is actually pointing at.

Set `IRFARAN_TOKEN` in the environment to choose your own instead, in which case that value is used, the generated one is ignored, and it is never displayed at all — you picked it, so you already have it.

It is a doorstop, not a security model: it stops a misbehaving tracker or a stray `curl` from altering history. It does not protect reads, which are open to anyone who can reach the app — so run it somewhere only you can.

PMTiles is read over HTTP range requests, so the archive doesn't have to sit on the same machine — point the basemap at any host that serves ranges.

## Workout trackers (optional)

Services that already hold your activities. Nothing is pushed here — Irfaran asks, either on a timer or when you press **Sync now** under Settings → Data sources → Workout trackers.

**intervals.icu.** Paste an API key from intervals.icu → Settings → Developer, leave the athlete as `0` for whoever the key belongs to, and set how often to check. Zero hours means never on its own, only the button.

Activities are read from intervals.icu's sample streams and filed under the same `workout` source a file drop uses, so anything already imported by hand is recognised and skipped rather than drawn twice. Sessions with no GPS — indoor trainer rides, pool swims — are skipped without being downloaded at all, because the activity listing already says which streams exist.

A sync reports its progress as it runs: how many activities it is checking, which one it is on, and how many are new so far.

The key is stored on the server and never sent back to any browser, so the field is blank every time you open it; leaving it blank keeps the key already saved. It is listed in `SECRET_SETTINGS` alongside the app's own token, which means no endpoint will return it.

## Live tracking (optional)

All live sources are off by default and toggled independently under Settings → Data sources. Irfaran works fine with none of them enabled.

There's no custom app to install and no plugin. Each source is a small HTTP endpoint that an existing tracker app posts to.

| Source | Density | Offline buffering | Best for |
|---|---|---|---|
| **Overland** | configurable, up to continuous | **yes** | general use — recommended |
| **OwnTracks** | 100 m / 300 s in Move mode | no | cross-platform, battery-conscious setups |
| **Home Assistant** | coarse, event-driven | no | already running HA and want no extra apps |

### Overland (recommended)

[Overland](https://overland.p3k.app/) is a free iOS GPS logger that posts batched GeoJSON to an endpoint of your choosing. It records while offline and delivers later, so tunnels and dead zones don't punch holes in your track.

1. Install Overland and grant location permission **Always**.
2. Settings → slide to unlock → Receiver Endpoint URL:
   `http://irfaran.internal:8000/api/ingest/overland`
3. Set the access token to your Irfaran token — Overland sends it as an `Authorization` header.
4. Optionally set a Device ID.
5. Choose *reduced* resolution limited by distance for a sane battery/detail trade-off. *Significant Location Only* uses almost no battery but yields neighbourhood-level dots rather than routes.
6. Enable the Overland toggle in Irfaran's settings.

Each point carries a motion state — walking, running, driving, cycling or stationary — which Irfaran stores alongside it.

iOS does not let any app guarantee a collection interval. Overland's own documentation is blunt about this: you can suggest what you want from CoreLocation and take what you get.

### OwnTracks

[OwnTracks](https://owntracks.org/) works on iOS and Android and offers four monitoring modes.

1. Install OwnTracks and open settings via the **i** icon, top left.
2. Choose **HTTP** mode and set the URL to `http://irfaran.internal:8000/api/ingest/owntracks`.
3. Add the token through the `httpHeaders` setting (iOS only) as `X-Irfaran-Token:<token>`.
4. Enable the OwnTracks toggle in Irfaran's settings.

**Mode choice matters.** *Significant changes* uses Apple's low-power API, firing roughly every 500 m or 5 minutes — fine for presence, too sparse for routes. *Move* mode polls continuously and publishes whenever you've moved `locatorDisplacement` metres or `locatorInterval` seconds have elapsed, defaulting to 100 m / 300 s and adjustable in iOS Settings. Move mode draws battery comparable to a navigation app.

The useful trick is geofence-driven mode switching: name a region `Home|1|2` and OwnTracks flips to Move mode on exit and back to Significant on entry. The `downgrade` setting drops out of Move mode below a battery threshold, and `adapt` returns to Significant after a period of stillness. OwnTracks' docs note that automatic switching into Move mode isn't fully reliable and suggest adding a `+follow` region as a wake-up nudge.

### Home Assistant

No custom component needed. The Companion app already reports location to HA as a `device_tracker` entity; an automation forwards each new fix to Irfaran.

**1. `configuration.yaml`:**

```yaml
rest_command:
  irfaran_point:
    url: http://irfaran.internal:8000/api/ingest/ha
    method: POST
    headers:
      X-Irfaran-Token: !secret irfaran_token
      Content-Type: application/json
    payload: >
      {"lat": {{ lat }}, "lon": {{ lon }},
       "accuracy": {{ accuracy }}, "timestamp": "{{ ts }}",
       "device": "{{ device }}"}
    timeout: 10
```

Put the token in `secrets.yaml` as `irfaran_token`, matching Irfaran's `.env`.

**2. The automation:**

```yaml
automation:
  - alias: Irfaran location push
    mode: queued
    max: 25
    triggers:
      - trigger: state
        entity_id: device_tracker.my_phone
        attribute: latitude
    conditions:
      - condition: template
        value_template: >
          {{ state_attr(trigger.entity_id, 'gps_accuracy') | float(999) <= 50 }}
    actions:
      - action: rest_command.irfaran_point
        data:
          lat: "{{ state_attr(trigger.entity_id, 'latitude') }}"
          lon: "{{ state_attr(trigger.entity_id, 'longitude') }}"
          accuracy: "{{ state_attr(trigger.entity_id, 'gps_accuracy') }}"
          ts: "{{ now().isoformat() }}"
          device: "{{ trigger.entity_id }}"
```

This uses the `triggers:` / `conditions:` / `actions:` syntax from HA 2024.10. On older versions use `trigger:` / `condition:` / `action:` with `platform: state` and `service: rest_command.irfaran_point`.

`mode: queued` is load-bearing — updates arrive in bursts and the default `single` mode silently drops the overlapping ones.

**Understand what HA can and cannot give you.** The Companion app is event-driven: it reports on zone transitions, app open, throttled background fetch, an explicit notification request, and significant location change. There is no interval setting. On a motorway the only trigger firing is significant location change — Apple's cell-tower-handoff API — so a long drive produces fixes kilometres apart joined by straight lines. HA also doesn't retry failed `rest_command` calls, and its recorder purges after roughly ten days, so a push that fails while Irfaran is down is a permanently lost point.

Treat HA as ambient coverage — "I was in this city, this neighbourhood" — not as a route recorder. Where the road matters, use Overland or record a GPX.

### Shared behaviour

- **Off by default, and refused clearly while off.** A disabled endpoint answers `503` with a message naming the toggle. It never accepts a fix silently and never returns a bare `404`, so a misconfigured tracker tells you what is wrong instead of appearing to work.
- **Accuracy filtering is server-side and authoritative.** Fixes worse than 50 m are dropped regardless of what the client sent. Indoor and underground positions routinely report 100 m or worse and would otherwise produce fog blobs where you sat still.
- **Sparse fixes are interpolated** as straight lines between consecutive points, so tracks cut corners rather than tracing your exact path.
- **One track per source per day, not one event per fix.** Fixes append to that day's open track, so a day of tracking is a single event holding a growing line rather than several thousand rows and a map made of dots.
- **Late and repeated batches are handled.** Points are held in time order and deduplicated on their timestamp, so a phone that spent an hour in a tunnel can deliver what it recorded in any order and still produce one continuous track. Redelivering a batch changes nothing.
- **Appending rebuilds rather than paints on top.** A day's tiles are rebuilt from the event log each time it grows, so what live tracking produces is byte for byte what a full rebuild produces.
- **Use a hostname reachable from the tracker**, not `localhost`. If Irfaran and HA both run in containers on one host, use the LAN address or a shared Docker network alias.
- **Token.** Every live endpoint needs the shared token as `X-Irfaran-Token`. Overland cannot send arbitrary headers, so it may use its own `Authorization: Bearer <token>` instead — both are accepted on that endpoint.

## Moving to another machine

Settings → Backup exports one file holding everything that cannot be derived: the event
log, pins, labels, folders, who-was-there names, and how the map is set to look. Not the blobs or tiles —
those are rebuilt from the events, byte for byte — and not the basemap, which is public
map data the new instance can fetch itself. A 1,244-event archive is about 5 MB.

Import merges rather than replaces: nothing is deleted, nothing is overwritten, and
importing the same file twice changes nothing the second time. A fresh instance offers
it right on the setup screen.

Your API token does not travel. It belongs to the server, not to the archive.

## Backups

Almost everything in the data directory is derived and disposable. Back up the small part and let the rest rebuild.

**Back this up:**

| | |
|---|---|
| `data/irfaran.db` | the event log, places and settings — the only thing that cannot be recreated |
| your original GPX and TCX files | wherever you keep them; Irfaran stores the events it derived, not the files |

**Do not back this up:**

| | |
|---|---|
| `data/planet.pmtiles` | ~128 GB of public map data, re-downloadable from the setup screen |
| `data/tiles/` | rendered PNGs, rebuilt by `render` |
| the `blobs` table inside `irfaran.db` | bitmaps, rebuilt by `rebuild` |

The blobs live inside the database, so a plain file copy takes them along. That is harmless — it just makes the backup larger than it needs to be. To make it small, vacuum them out of a copy:

```bash
sqlite3 data/irfaran.db ".backup /tmp/irfaran-backup.db"
sqlite3 /tmp/irfaran-backup.db "DELETE FROM blobs; VACUUM;"
```

To restore: put the database back, then rebuild what was thrown away.

```bash
docker compose exec api python -m irfaran.cli rebuild
docker compose exec api python -m irfaran.cli render
```

`rebuild` replays the event log into bitmaps and `render` redraws the tiles. Both are deterministic — the same event log always produces the same bytes — so a restored backup is indistinguishable from the original.

## A note on privacy

This application stores a detailed record of where you and your family have been. Run it on an internal network. Don't expose it to the internet without an identity-aware proxy in front of it. Write endpoints require a shared token, which is a doorstop, not a security model.

If you contribute: **never commit real location data**. Test fixtures must be synthetic. Screenshots must use fake data. A fog map centred on someone's home is a map to their home.

## How this was built

The specification, architecture and all design decisions are mine. The implementation is written by **[Claude](https://claude.com/claude-code)** working against a detailed written spec, phase by phase, with each phase reviewed and tested before the next begins.

I'm saying this plainly because it's true and because it's relevant if you're reading the code: it was produced by an AI agent following a human-authored design, not typed by hand. The architecture decisions — the z14 raster grid, the event log as source of truth, erase-as-composite-mask — came out of a long design conversation and are documented in the build plan. The code that implements them did not.

Bugs are still mine.

## The website

A one-page site lives in [`docs/`](docs/), ready for GitHub Pages — enable it under
**Settings → Pages** with the source set to the `main` branch and the `/docs` folder.

Every image on it is rendered from invented data by the two scripts in
[`docs/img/`](docs/img/): a seeded random walk on a street grid in the middle of the
Atlantic. Nothing there shows anyone's real movements, and nothing should ever be
replaced with a picture that does.

## Credits

- [Fog of World](https://fogofworld.app/) — the original, and the source of the z14 grid design
- [Overland](https://overland.p3k.app/) and [OwnTracks](https://owntracks.org/) — the tracker apps that make live ingest possible
- [Dawarich](https://github.com/Freika/dawarich) — self-hosted location history, prior art worth a look
- [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors — map data, ODbL
- [Protomaps](https://protomaps.com) — basemap tiles
- [MapLibre GL JS](https://maplibre.org/) — map rendering

## License

[GNU Affero General Public License v3.0](LICENSE).
