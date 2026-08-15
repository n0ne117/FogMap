// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The map is three stacked layers: basemap, then trails, then fog on top.
// Fog is opaque where the ground is unexplored, so it has to be drawn last.

import { layers, namedFlavor } from '@protomaps/basemaps'
import { Map as MapLibreMap, addProtocol } from 'maplibre-gl'
import type { MapOptions as MapLibreMapOptions } from 'maplibre-gl'
import { Protocol } from 'pmtiles'

import type { MapTheme } from './theme'

/** MapLibre 6 exports no StyleSpecification, so take it from the constructor. */
type StyleSpec = Exclude<MapLibreMapOptions['style'], string | undefined>

const BASEMAP_FILE = 'planet.pmtiles'
const BASEMAP_URL = `/api/basemap/${BASEMAP_FILE}`

// Protomaps hosts the glyphs and sprites its styles reference. They are the
// one thing on this page not served by this deployment.
export const ASSETS = 'https://protomaps.github.io/basemaps-assets'

const FOG_SOURCE = 'fogmap-fog'
const TRAIL_SOURCE = 'fogmap-trail'
const BASEMAP_SOURCE = 'protomaps'
const FOG_LAYER = 'fogmap-fog'
const FOG_OPACITY_KEY = 'fogmap.fog.opacity'

// Not baked into the tiles. How much of the map shows through the fog is a
// viewing preference, and MapLibre scales a raster layer on the GPU - instant,
// free, and no re-render. Baking one answer in would also be wrong for the
// other theme: near-black fog over a dark basemap needs far more transparency
// than pale fog over a light one to read at all.
const DEFAULT_FOG_OPACITY = 0.6

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
      // The native grid is z14. Above that the client overzooms rather than
      // the server rendering tiles that carry no more information.
      maxzoom: 14,
    },
    [FOG_SOURCE]: {
      type: 'raster',
      tiles: [rasterTiles(setup.theme, setup.view, 'fog')],
      tileSize: 256,
      minzoom: 0,
      maxzoom: 14,
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
      { id: 'fogmap-trail', type: 'raster', source: TRAIL_SOURCE },
      {
        id: FOG_LAYER,
        type: 'raster',
        source: FOG_SOURCE,
        paint: { 'raster-opacity': getFogOpacity() },
      },
    ],
  } as unknown as StyleSpec
}

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
    addProtocol('pmtiles', new Protocol().tile)
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
