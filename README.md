# FogMap

A self-hosted fog-of-war map of everywhere you've been.

Feed it GPS tracks — live from Overland, OwnTracks or Home Assistant, imported from workout files, or drawn by hand for the years before GPS existed — and it renders a persistent map where the places you've visited are revealed and everywhere else stays under fog. Inspired by [Fog of World](https://fogofworld.app/), but self-hosted, on your own hardware, with your data staying on it.

**Status:** early development. See [CHANGELOG.md](CHANGELOG.md) for what works right now and [ROADMAP.md](ROADMAP.md) for what's coming.

---

## How it works

The design goal is that the map stays fast no matter how much history accumulates. Everything below follows from that.

Every GPS track is rasterised once, at import, into a persistent bitmap on a web-Mercator z14 grid — 256×256 pixel tiles, about 9.5 m per pixel at the equator. Serving a tile is a file read. Nothing is rendered at request time.

The consequence is that **display cost is independent of dataset size**. A tile takes the same time to serve whether it was built from fifty points or fifty million. Zooming and panning stay fast as the archive grows, which is the entire point of the design.

Three layers, each derived from the one above it:

1. **An append-only event log** — the only source of truth. Every import and every brush stroke is an event with its own geometry, radius and time layer.
2. **z14 bitmap blobs** — derived, disposable.
3. **A pre-rendered PNG pyramid** — derived, disposable.

Delete both caches, run a rebuild, get byte-identical output. That means the whole archive can be reprocessed with different brush radii whenever you change your mind, which turns out to matter a lot when you're reconstructing places from forty years ago and your relatives keep correcting each other.

## Features

- Raster fog-of-war rendering, fast at every zoom level
- Trail layer showing route frequency, so your regular loops stand out from one-off trips
- Live tracking via Overland, OwnTracks or Home Assistant (all optional, off by default)
- GPX and TCX import
- Per-year subdivision maps plus a cumulative all-time view
- Manual brush: reveal fog for places you know you've been, erase fog wrongly cleared by GPS drift
- Freehand and point-to-point route drawing for pre-digital history
- Labelled places with categories and who was there
- Independent light/dark themes for the interface and the map

## Quick start

Requires Docker and Docker Compose. Both machines this was developed against are amd64.

### Run from published images

```bash
curl -O https://raw.githubusercontent.com/<owner>/fogmap/main/docker-compose.prod.yml
cp .env.example .env    # edit it
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

Images are published to GitHub Container Registry as `ghcr.io/<owner>/fogmap-api` and `ghcr.io/<owner>/fogmap-web`. Pin an explicit version tag rather than `latest`.

### Build from source

```bash
git clone https://github.com/<owner>/fogmap.git
cd fogmap
cp .env.example .env
docker compose build
docker compose run --rm test
docker compose up -d
docker compose exec api python -m fogmap.cli selfcheck
```

On Fedora or any SELinux-enforcing host, bind mounts need a `:z` label. The bundled compose files already have it.

### Basemap

The map needs a [Protomaps](https://protomaps.com) PMTiles basemap. The full planet at z0–15 is roughly 120 GB; regional extracts are much smaller. PMTiles is served over HTTP range requests, so it doesn't have to sit on the same machine.

## Live tracking (optional)

All live sources are off by default and toggled independently under Settings → Data sources. FogMap works fine with none of them enabled.

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
   `http://fogmap.internal:8000/api/ingest/overland`
3. Set the access token to your FogMap token — Overland sends it as an `Authorization` header.
4. Optionally set a Device ID.
5. Choose *reduced* resolution limited by distance for a sane battery/detail trade-off. *Significant Location Only* uses almost no battery but yields neighbourhood-level dots rather than routes.
6. Enable the Overland toggle in FogMap's settings.

Each point carries a motion state — walking, running, driving, cycling or stationary — which FogMap stores alongside it.

iOS does not let any app guarantee a collection interval. Overland's own documentation is blunt about this: you can suggest what you want from CoreLocation and take what you get.

### OwnTracks

[OwnTracks](https://owntracks.org/) works on iOS and Android and offers four monitoring modes.

1. Install OwnTracks and open settings via the **i** icon, top left.
2. Choose **HTTP** mode and set the URL to `http://fogmap.internal:8000/api/ingest/owntracks`.
3. Add the token through the `httpHeaders` setting (iOS only) as `X-FogMap-Token:<token>`.
4. Enable the OwnTracks toggle in FogMap's settings.

**Mode choice matters.** *Significant changes* uses Apple's low-power API, firing roughly every 500 m or 5 minutes — fine for presence, too sparse for routes. *Move* mode polls continuously and publishes whenever you've moved `locatorDisplacement` metres or `locatorInterval` seconds have elapsed, defaulting to 100 m / 300 s and adjustable in iOS Settings. Move mode draws battery comparable to a navigation app.

The useful trick is geofence-driven mode switching: name a region `Home|1|2` and OwnTracks flips to Move mode on exit and back to Significant on entry. The `downgrade` setting drops out of Move mode below a battery threshold, and `adapt` returns to Significant after a period of stillness. OwnTracks' docs note that automatic switching into Move mode isn't fully reliable and suggest adding a `+follow` region as a wake-up nudge.

### Home Assistant

No custom component needed. The Companion app already reports location to HA as a `device_tracker` entity; an automation forwards each new fix to FogMap.

**1. `configuration.yaml`:**

```yaml
rest_command:
  fogmap_point:
    url: http://fogmap.internal:8000/api/ingest/ha
    method: POST
    headers:
      X-FogMap-Token: !secret fogmap_token
      Content-Type: application/json
    payload: >
      {"lat": {{ lat }}, "lon": {{ lon }},
       "accuracy": {{ accuracy }}, "timestamp": "{{ ts }}",
       "device": "{{ device }}"}
    timeout: 10
```

Put the token in `secrets.yaml` as `fogmap_token`, matching FogMap's `.env`.

**2. The automation:**

```yaml
automation:
  - alias: FogMap location push
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
      - action: rest_command.fogmap_point
        data:
          lat: "{{ state_attr(trigger.entity_id, 'latitude') }}"
          lon: "{{ state_attr(trigger.entity_id, 'longitude') }}"
          accuracy: "{{ state_attr(trigger.entity_id, 'gps_accuracy') }}"
          ts: "{{ now().isoformat() }}"
          device: "{{ trigger.entity_id }}"
```

This uses the `triggers:` / `conditions:` / `actions:` syntax from HA 2024.10. On older versions use `trigger:` / `condition:` / `action:` with `platform: state` and `service: rest_command.fogmap_point`.

`mode: queued` is load-bearing — updates arrive in bursts and the default `single` mode silently drops the overlapping ones.

**Understand what HA can and cannot give you.** The Companion app is event-driven: it reports on zone transitions, app open, throttled background fetch, an explicit notification request, and significant location change. There is no interval setting. On a motorway the only trigger firing is significant location change — Apple's cell-tower-handoff API — so a long drive produces fixes kilometres apart joined by straight lines. HA also doesn't retry failed `rest_command` calls, and its recorder purges after roughly ten days, so a push that fails while FogMap is down is a permanently lost point.

Treat HA as ambient coverage — "I was in this city, this neighbourhood" — not as a route recorder. Where the road matters, use Overland or record a GPX.

### Shared behaviour

- **Accuracy filtering is server-side and authoritative.** Fixes worse than 50 m are dropped regardless of what the client sent. Indoor and underground positions routinely report 100 m or worse and would otherwise produce fog blobs where you sat still.
- **Sparse fixes are interpolated** as straight lines between consecutive points, so tracks cut corners rather than tracing your exact path.
- **Use a hostname reachable from the tracker**, not `localhost`. If FogMap and HA both run in containers on one host, use the LAN address or a shared Docker network alias.

## A note on privacy

This application stores a detailed record of where you and your family have been. Run it on an internal network. Don't expose it to the internet without an identity-aware proxy in front of it. Write endpoints require a shared token, which is a doorstop, not a security model.

If you contribute: **never commit real location data**. Test fixtures must be synthetic. Screenshots must use fake data. A fog map centred on someone's home is a map to their home.

## How this was built

The specification, architecture and all design decisions are mine. The implementation is written by **[Claude](https://claude.com/claude-code)** working against a detailed written spec, phase by phase, with each phase reviewed and tested before the next begins.

I'm saying this plainly because it's true and because it's relevant if you're reading the code: it was produced by an AI agent following a human-authored design, not typed by hand. The architecture decisions — the z14 raster grid, the event log as source of truth, erase-as-composite-mask — came out of a long design conversation and are documented in the build plan. The code that implements them did not.

Bugs are still mine.

## Credits

- [Fog of World](https://fogofworld.app/) — the original, and the source of the z14 grid design
- [Overland](https://overland.p3k.app/) and [OwnTracks](https://owntracks.org/) — the tracker apps that make live ingest possible
- [Dawarich](https://github.com/Freika/dawarich) — self-hosted location history, prior art worth a look
- [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors — map data, ODbL
- [Protomaps](https://protomaps.com) — basemap tiles
- [MapLibre GL JS](https://maplibre.org/) — map rendering

## License

[GNU Affero General Public License v3.0](LICENSE).
