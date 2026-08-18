// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The map is three stacked layers: basemap, then trails, then fog on top.
// Fog is opaque where the ground is unexplored, so it has to be drawn last.

import { layers, namedFlavor } from '@protomaps/basemaps'
import { Map as MapLibreMap, addProtocol } from 'maplibre-gl'
import type {
  DataDrivenPropertyValueSpecification,
  MapOptions as MapLibreMapOptions,
} from 'maplibre-gl'
import { PMTiles, Protocol } from 'pmtiles'

import type { MapTheme } from './theme'

/** MapLibre 6 exports no StyleSpecification, so take it from the constructor. */
type StyleSpec = Exclude<MapLibreMapOptions['style'], string | undefined>

const BASEMAP_FILE = 'planet.pmtiles'
const BASEMAP_URL = `/api/basemap/${BASEMAP_FILE}`

// Protomaps hosts the glyphs and sprites its styles reference. They are the
// one thing on this page not served by this deployment.
export const ASSETS = 'https://protomaps.github.io/basemaps-assets'

const FOG_SOURCE = 'irfaran-fog'
const TRAIL_SOURCE = 'irfaran-trail'
const BASEMAP_SOURCE = 'protomaps'
const FOG_LAYER = 'irfaran-fog'
const BORDERS_LAYER = 'irfaran-borders'
const FOG_OPACITY_KEY = 'irfaran.fog.opacity'
const BORDERS_KEY = 'irfaran.borders'
const HEAT_KEY = 'irfaran.trail.opacity'
const TRAIL_LAYER = 'irfaran-trail'

/** Deepest zoom the server renders fog and trail tiles at. Matches geo.MAX_Z. */
export const MAX_RENDERED_ZOOM = 16

/** How long a fog or trail tile takes to cross-fade in, in milliseconds.
 *
 * MapLibre's default is 300 ms, and on top of the fetch that made the fog look
 * slow to arrive: the tiles were there, they were still fading. Fog is a flat
 * wash of one colour, so there is nothing for a cross-fade to smooth over -
 * it only delays the answer. Raise this if tile edges popping in while zooming
 * bothers you more than the wait does.
 */
const TILE_FADE_MS = 0

/**
 * How strong the trail colouring is, as a multiplier on its own fade.
 *
 * A viewing choice, like fog thickness: MapLibre scales the raster layer on
 * the GPU, so it changes as the slider moves and nothing is re-rendered.
 *
 * Half strength by default. At full strength the colouring is the loudest
 * thing on the map and drowns the streets it is drawn over; at half it still
 * reads as how often you went somewhere, over a basemap you can still see.
 */
const DEFAULT_HEAT_OPACITY = 0.5

export function getHeatOpacity(): number {
  try {
    const stored = window.localStorage.getItem(HEAT_KEY)
    if (stored !== null) {
      const value = Number(stored)
      if (Number.isFinite(value) && value >= 0 && value <= 1) return value
    }
  } catch {
    /* fall through to the default */
  }
  return DEFAULT_HEAT_OPACITY
}

export function setHeatOpacity(map: MapLibreMap, opacity: number): void {
  try {
    window.localStorage.setItem(HEAT_KEY, String(Math.max(0, Math.min(1, opacity))))
  } catch {
    /* remembering is optional */
  }
  applyHeatOpacity(map)
}

/**
 * The trail raster's own zoom fade, scaled by the strength setting.
 *
 * The factor is folded into the interpolation's output values rather than
 * multiplied over the whole expression. MapLibre requires `zoom` to be the
 * input of a *top-level* interpolate: wrapping one in a multiply makes the
 * style invalid, and an invalid style is rejected whole - basemap, fog and
 * trails all at once, with nothing on screen to say why.
 */
function heatFade(): DataDrivenPropertyValueSpecification<number> {
  const strength = getHeatOpacity()
  return [
    'interpolate',
    ['linear'],
    ['zoom'],
    MAX_RENDERED_ZOOM,
    strength,
    MAX_RENDERED_ZOOM + 1.5,
    0.3 * strength,
  ]
}

export function applyHeatOpacity(map: MapLibreMap): void {
  if (!map.getLayer(TRAIL_LAYER)) return
  map.setPaintProperty(TRAIL_LAYER, 'raster-opacity', heatFade())
}

// Not baked into the tiles. How much of the map shows through the fog is a
// viewing preference, and MapLibre scales a raster layer on the GPU - instant,
// free, and no re-render. Baking one answer in would also be wrong for the
// other theme: near-black fog over a dark basemap needs far more transparency
// than pale fog over a light one to read at all.
const DEFAULT_FOG_OPACITY = 0.8

export function getFogOpacity(): number {
  try {
    const stored = window.localStorage.getItem(FOG_OPACITY_KEY)
    if (stored !== null) {
      const value = Number(stored)
      if (Number.isFinite(value) && value >= 0 && value <= 1) return value
    }
  } catch {
    /* fall through to the default */
  }
  return DEFAULT_FOG_OPACITY
}

/**
 * Country borders, drawn above the fog.
 *
 * The basemap already draws them, underneath - which at any usable fog
 * thickness means not at all. Repeating the layer on top is what makes the
 * shape of a country readable across ground nobody has visited, which is most
 * of the map and the whole point of looking at it zoomed out.
 */
export function getBordersVisible(): boolean {
  try {
    // On unless it was explicitly turned off. Country lines are how you tell
    // at a glance which country a cleared patch is in, which is most of what
    // a zoomed-out fog map is for.
    return window.localStorage.getItem(BORDERS_KEY) !== 'false'
  } catch {
    return true
  }
}

export function setBordersVisible(map: MapLibreMap, visible: boolean): void {
  try {
    window.localStorage.setItem(BORDERS_KEY, String(visible))
  } catch {
    /* a preference that cannot be stored is still worth applying now */
  }
  applyBorders(map)
}

export function applyBorders(map: MapLibreMap): void {
  if (!map.getLayer(BORDERS_LAYER)) return
  map.setLayoutProperty(
    BORDERS_LAYER,
    'visibility',
    getBordersVisible() ? 'visible' : 'none',
  )
}

export function setFogOpacity(map: MapLibreMap, opacity: number): void {
  const clamped = Math.max(0, Math.min(1, opacity))
  try {
    window.localStorage.setItem(FOG_OPACITY_KEY, String(clamped))
  } catch {
    /* remembering is optional */
  }
  applyFogOpacity(map)
}

/** Re-apply after any setStyle, which resets every paint property. */
export function applyFogOpacity(map: MapLibreMap): void {
  if (map.getLayer(FOG_LAYER)) {
    map.setPaintProperty(FOG_LAYER, 'raster-opacity', getFogOpacity())
  }
}

export interface MapSetup {
  container: string
  theme: MapTheme
  view: string
  hasBasemap: boolean
}

/** Is a basemap archive actually present? Asked once, before the map loads. */
export async function basemapAvailable(): Promise<boolean> {
  try {
    const response = await fetch(BASEMAP_URL, { method: 'HEAD' })
    return response.ok
  } catch {
    return false
  }
}

// Bumped after a stroke is saved. Tiles are cached hard by design, so without
// a changing token a freshly drawn route would not appear until the cache
// expired - which looks exactly like the drawing being lost.
let cacheBust = 0

export function bustTileCache(): void {
  cacheBust += 1
}

function rasterTiles(theme: MapTheme, view: string, kind: 'fog' | 'trail'): string {
  const base = `${window.location.origin}/api/tiles/${theme}/${view}/${kind}/{z}/{x}/{y}.png`
  return cacheBust === 0 ? base : `${base}?v=${cacheBust}`
}

/**
 * Build the whole style for a theme.
 *
 * Switching theme rebuilds this and hands it to setStyle. That changes the
 * basemap colours and the raster URLs and nothing else - no server-side render
 * is involved, because both themes were already rendered at ingest.
 */
export function buildStyle(setup: MapSetup): StyleSpec {
  const basemapLayers = setup.hasBasemap
    ? layers(BASEMAP_SOURCE, namedFlavor(setup.theme), { lang: 'en' })
    : [
        {
          id: 'background',
          type: 'background',
          paint: {
            'background-color': setup.theme === 'dark' ? '#34373d' : '#cccccc',
          },
        },
      ]

  const sources: Record<string, unknown> = {
    [TRAIL_SOURCE]: {
      type: 'raster',
      tiles: [rasterTiles(setup.theme, setup.view, 'trail')],
      tileSize: 256,
      minzoom: 0,
      // Everything is stored on the z14 grid, but the PNG pyramid is rendered
      // two levels deeper - stamped from the same geometry rather than
      // upscaled - because a 15 m brush is two pixels at z14 and magnifying
      // that to z18 is a smear. Must match geo.MAX_Z on the server.
      maxzoom: MAX_RENDERED_ZOOM,
    },
    [FOG_SOURCE]: {
      type: 'raster',
      tiles: [rasterTiles(setup.theme, setup.view, 'fog')],
      tileSize: 256,
      minzoom: 0,
      maxzoom: MAX_RENDERED_ZOOM,
    },
  }

  if (setup.hasBasemap) {
    sources[BASEMAP_SOURCE] = {
      type: 'vector',
      url: `pmtiles://${window.location.origin}${BASEMAP_URL}`,
      attribution:
        '<a href="https://openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }
  }

  return {
    version: 8,
    glyphs: `${ASSETS}/fonts/{fontstack}/{range}.pbf`,
    sprite: `${ASSETS}/sprites/v4/${setup.theme}`,
    sources,
    layers: [
      ...basemapLayers,
      {
        id: 'irfaran-trail',
        type: 'raster',
        source: TRAIL_SOURCE,
        // Sharp to z16, magnified past it. The vector trail layer draws the
        // same tracks from their real geometry, so the raster fades down as
        // that takes over - but not to nothing, because the raster is the only
        // thing carrying how many times a pixel was crossed, and at low
        // opacity it reads as a glow under the crisp line.
        paint: { 'raster-opacity': heatFade(), 'raster-fade-duration': TILE_FADE_MS },
      },
      {
        id: FOG_LAYER,
        type: 'raster',
        source: FOG_SOURCE,
        paint: {
          'raster-opacity': getFogOpacity(),
          'raster-fade-duration': TILE_FADE_MS,
        },
      },
      ...(setup.hasBasemap
        ? [
            {
              id: BORDERS_LAYER,
              type: 'line',
              source: BASEMAP_SOURCE,
              'source-layer': 'boundaries',
              // kind_detail 2 and below is the country line. Anything above
              // is a state or a county, which is not what was asked for and
              // turns a world view into a net.
              filter: ['<=', 'kind_detail', 2],
              layout: {
                'line-join': 'round',
                visibility: getBordersVisible() ? 'visible' : 'none',
              },
              paint: {
                'line-color': '#d8a13a',
                'line-width': ['interpolate', ['linear'], ['zoom'], 2, 0.8, 8, 1.6],
                'line-opacity': 0.85,
                'line-dasharray': [3, 2],
              },
            },
          ]
        : []),
    ],
  } as unknown as StyleSpec
}

/** Exposed so the archive can be read directly when the map will not draw. */
export function openArchive(): PMTiles {
  return new PMTiles(`${window.location.origin}${BASEMAP_URL}`)
}

/**
 * The pmtiles protocol, with archive metadata enabled.
 *
 * `metadata` defaults to false, and with it off the TileJSON handed to
 * MapLibre carries tiles, zooms and bounds but no `vector_layers`. MapLibre
 * needs those to resolve the `source-layer` every basemap layer references,
 * so without them the source is created, never finishes loading, draws
 * nothing and reports no error - a blank map that looks in every other way
 * healthy. Turning it on makes the archive's own nine layer definitions part
 * of the TileJSON.
 */
export const pmtilesProtocol = new Protocol({ metadata: true })

let protocolRegistered = false

export function spriteUrl(theme: MapTheme): string {
  return `${ASSETS}/sprites/v4/${theme}`
}

export function glyphsUrl(): string {
  return `${ASSETS}/fonts/{fontstack}/{range}.pbf`
}

export function createMap(setup: MapSetup): MapLibreMap {
  if (!protocolRegistered) {
    // This is how MapLibre learns to read a pmtiles:// url.
    addProtocol('pmtiles', pmtilesProtocol.tile)
    protocolRegistered = true
  }

  return new MapLibreMap({
    container: setup.container,
    style: buildStyle(setup),
    center: [0, 20],
    zoom: 2,
    maxZoom: 18,
    hash: true,
    attributionControl: {
      compact: true,
      customAttribution: [
        '<a href="https://protomaps.com" target="_blank" rel="noreferrer">Protomaps</a>',
        '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>',
      ],
    },
  })
}

export function applyMapTheme(map: MapLibreMap, setup: MapSetup): void {
  map.setStyle(buildStyle(setup), { diff: false })
}

/**
 * Point the fog and trail sources at a different view.
 *
 * Only the raster URLs change - the basemap is left alone, so stepping through
 * years does not reload it. Both views were rendered at ingest, so this is a
 * cache lookup away from instant.
 */
export function applyView(map: MapLibreMap, setup: MapSetup): void {
  for (const [id, kind] of [
    [TRAIL_SOURCE, 'trail'],
    [FOG_SOURCE, 'fog'],
  ] as const) {
    const source = map.getSource(id)
    if (source && 'setTiles' in source) {
      ;(source as { setTiles: (tiles: string[]) => unknown }).setTiles([
        rasterTiles(setup.theme, setup.view, kind),
      ])
    }
  }
}
