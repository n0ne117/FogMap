// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The vector trail layer. Above z14 the raster has less detail than the
// geometry behind it, so the individual lines are worth sending - and only
// there. This is the one response in FogMap allowed to grow with the data,
// and it is bounded by the viewport and a hard cap.

import { Popup } from 'maplibre-gl'
import type {
  DataDrivenPropertyValueSpecification,
  Map as MapLibreMap,
} from 'maplibre-gl'

import { apiGet } from './api'

export const MIN_TRAIL_ZOOM = 14

/**
 * How the individual tracks are drawn.
 *
 *   detailed  one legible line per track, with a casing under it. Right when
 *             there are a handful; a white wall when there are a hundred.
 *   faint     hairlines at low opacity and no casing, so overlapping tracks
 *             accumulate instead of covering each other. A street crossed
 *             once is a whisper, a street crossed daily is bright: the
 *             density becomes the picture rather than destroying it.
 *   auto      whichever of those suits how much is on screen.
 *   off       none. The trail bitmap underneath still carries the passes.
 */
export type TrailStyle = 'auto' | 'detailed' | 'faint' | 'off'

const STYLE_KEY = 'fogmap.trails.style'
const POPUP_KEY = 'fogmap.trails.popups'

/**
 * Tracks in view above which "auto" stops drawing them individually.
 *
 * Not a measurement of anything - it is the point where counting the lines
 * stops being possible, which is when drawing them separately stops being
 * worth the ink.
 */
const DENSE_FROM = 24

export function getTrailStyle(): TrailStyle {
  try {
    const stored = window.localStorage.getItem(STYLE_KEY)
    if (stored === 'detailed' || stored === 'faint' || stored === 'off') return stored
  } catch {
    /* the default is a fine answer */
  }
  return 'auto'
}

export function setTrailStyle(value: TrailStyle): void {
  try {
    window.localStorage.setItem(STYLE_KEY, value)
  } catch {
    /* a preference that cannot be stored is still worth applying now */
  }
}

export function getTrailPopups(): boolean {
  try {
    return window.localStorage.getItem(POPUP_KEY) !== 'false'
  } catch {
    return true
  }
}

export function setTrailPopups(value: boolean): void {
  try {
    window.localStorage.setItem(POPUP_KEY, String(value))
  } catch {
    /* as above */
  }
}

/**
 * Where the vector lines take over from the trail bitmap.
 *
 * The bitmap is rendered natively to z16, so up to there it is sharper than a
 * styled line and carries the pass count as colour. Past it the bitmap starts
 * being magnified, and the real geometry is the better thing to look at.
 */
const FADE_IN: DataDrivenPropertyValueSpecification<number> = [
  'interpolate',
  ['linear'],
  ['zoom'],
  16,
  0,
  17.5,
  1,
]

/** The faint end of the same cross-fade, low enough that overlap accumulates. */
const FADE_IN_FAINT: DataDrivenPropertyValueSpecification<number> = [
  'interpolate',
  ['linear'],
  ['zoom'],
  16,
  0,
  17.5,
  0.14,
]

const SOURCE = 'fogmap-trail-vector'
const CASING_LAYER = 'fogmap-trail-casing'
const LAYER = 'fogmap-trail-lines'
const HIT_LAYER = 'fogmap-trail-hit'

interface TrailProperties {
  id: number
  source: string
  layers: string[]
  radius_m: number
  created_at: string
  meta: Record<string, unknown> | null
}

interface TrailCollection {
  type: 'FeatureCollection'
  features: { type: 'Feature'; id: number; geometry: unknown; properties: TrailProperties }[]
  truncated: boolean
  cap: number
}

const EMPTY = { type: 'FeatureCollection', features: [] } as const

function describe(properties: TrailProperties): string {
  const meta =
    typeof properties.meta === 'string'
      ? (JSON.parse(properties.meta) as Record<string, unknown>)
      : properties.meta

  const rows: string[] = []
  const title = (meta?.track as string) ?? (meta?.place as string) ?? properties.source
  rows.push(`<strong>${escapeHtml(String(title))}</strong>`)

  const bits: string[] = [properties.source]
  const layers = Array.isArray(properties.layers)
    ? properties.layers
    : (JSON.parse(String(properties.layers)) as string[])
  bits.push(layers.join(', '))
  rows.push(`<span class="pill">${escapeHtml(bits.join(' · '))}</span>`)

  if (meta?.started_at) {
    rows.push(
      `<div class="place-dates">${escapeHtml(String(meta.started_at).slice(0, 16).replace('T', ' '))}</div>`,
    )
  }
  const detail: string[] = []
  if (meta?.fixes) detail.push(`${meta.fixes} fixes`)
  if (meta?.activity) detail.push(String(meta.activity))
  if (meta?.motion) detail.push(String((meta.motion as string[]).join(', ')))
  detail.push(`${properties.radius_m} m brush`)
  rows.push(`<div class="place-people">${escapeHtml(detail.join(' · '))}</div>`)

  return `<div class="place-popup">${rows.join('')}</div>`
}

function escapeHtml(value: string): string {
  const div = document.createElement('div')
  div.textContent = value
  return div.innerHTML
}

export class Trails {
  private readonly map: MapLibreMap
  private readonly onStatus: (message: string) => void
  private attached = false
  private pending = 0
  /** Tracks the last refresh found in view, so a restyle needs no round trip. */
  private inView = 0
  view = 'all'

  constructor(map: MapLibreMap, onStatus: (message: string) => void) {
    this.map = map
    this.onStatus = onStatus
  }

  /**
   * Add the source and layers. Safe to call more than once.
   *
   * Only ever valid once the style has loaded: a map is not ready the instant
   * its constructor returns, and adding a source before then throws "Style is
   * not done loading" - which, called from start(), took everything wired
   * after it down with it. The caller attaches on style.load; isStyleLoaded()
   * is not that condition, it also waits on every source, so using it here
   * would make each attached source block whatever attaches next.
   */
  attach(): void {
    if (this.map.getSource(SOURCE)) return

    this.map.addSource(SOURCE, { type: 'geojson', data: EMPTY as never })

    // A dark casing under the line. White-on-white was legible over fog and
    // almost invisible the moment it crossed cleared ground, which is exactly
    // where a track is worth seeing.
    this.map.addLayer({
      id: CASING_LAYER,
      type: 'line',
      source: SOURCE,
      minzoom: MIN_TRAIL_ZOOM,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': 'rgba(0, 0, 0, 0.55)',
        'line-width': ['interpolate', ['linear'], ['zoom'], 14, 3, 18, 5, 22, 7],
        'line-opacity': FADE_IN,
      },
    })
    this.map.addLayer({
      id: LAYER,
      type: 'line',
      source: SOURCE,
      minzoom: MIN_TRAIL_ZOOM,
      // Round joins and caps, so a track reads as one continuous line rather
      // than a chain of segments with notches at every bend.
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': '#ffffff',
        // Thin, but not so thin it has to be hunted for. At z18 this is under
        // two metres of ground.
        'line-width': ['interpolate', ['linear'], ['zoom'], 14, 1, 18, 2.2, 22, 3],
        // Fades in as the raster underneath fades down, so the handover is a
        // cross-fade rather than both being drawn at full strength.
        'line-opacity': FADE_IN,
      },
    })
    // A wider invisible line, so clicking near a track counts as clicking it.
    this.map.addLayer({
      id: HIT_LAYER,
      type: 'line',
      source: SOURCE,
      minzoom: MIN_TRAIL_ZOOM,
      paint: { 'line-color': '#ffffff', 'line-width': 14, 'line-opacity': 0 },
    })

    if (!this.attached) {
      this.attached = true
      this.map.on('moveend', () => void this.refresh())
      this.map.on('click', HIT_LAYER, (event) => this.identify(event as never))
      this.map.on('mouseenter', HIT_LAYER, () => {
        if (!getTrailPopups() || getTrailStyle() === 'off') return
        this.map.getCanvas().style.cursor = 'pointer'
      })
      this.map.on('mouseleave', HIT_LAYER, () => {
        this.map.getCanvas().style.cursor = ''
      })
    }
  }

  private identify(event: { lngLat: unknown; features?: { properties: TrailProperties }[] }): void {
    if (!getTrailPopups() || getTrailStyle() === 'off') return

    const feature = event.features?.[0]
    if (!feature) return

    new Popup({ offset: 8 })
      .setLngLat(event.lngLat as never)
      .setHTML(describe(feature.properties))
      .addTo(this.map)
  }

  private setData(collection: unknown): void {
    const source = this.map.getSource(SOURCE)
    if (source && 'setData' in source) {
      ;(source as { setData: (data: unknown) => void }).setData(collection)
    }
  }

  /**
   * Draw the lines to suit how many of them there are.
   *
   * Called with the number of tracks in view, because that is the thing that
   * decides whether one line per track is information or noise. Overlapping
   * hairlines at low opacity add up, which turns a hundred tracks down one
   * street from a white wall into a bright line - the same fact, legibly.
   */
  restyle(inView: number = this.inView): void {
    if (!this.map.getLayer(LAYER)) return
    this.inView = inView

    const chosen = getTrailStyle()
    const style =
      chosen === 'auto' ? (inView > DENSE_FROM ? 'faint' : 'detailed') : chosen

    const on = style !== 'off'
    const faint = style === 'faint'

    this.map.setLayoutProperty(CASING_LAYER, 'visibility', on && !faint ? 'visible' : 'none')
    this.map.setLayoutProperty(LAYER, 'visibility', on ? 'visible' : 'none')
    this.map.setLayoutProperty(HIT_LAYER, 'visibility', on ? 'visible' : 'none')
    if (!on) return

    this.map.setPaintProperty(
      LAYER,
      'line-width',
      faint
        ? ['interpolate', ['linear'], ['zoom'], 14, 0.8, 18, 1.4, 22, 2]
        : ['interpolate', ['linear'], ['zoom'], 14, 1, 18, 2.2, 22, 3],
    )
    this.map.setPaintProperty(
      LAYER,
      'line-opacity',
      faint ? FADE_IN_FAINT : FADE_IN,
    )
  }

  async refresh(): Promise<void> {
    if (!this.map.getSource(SOURCE)) return

    if (this.map.getZoom() < MIN_TRAIL_ZOOM) {
      this.setData(EMPTY)
      this.onStatus('')
      return
    }

    const bounds = this.map.getBounds()
    const bbox = [
      bounds.getWest(),
      bounds.getSouth(),
      bounds.getEast(),
      bounds.getNorth(),
    ]
      .map((value) => value.toFixed(6))
      .join(',')

    const layer = this.view.startsWith('year:')
      ? this.view.slice('year:'.length)
      : this.view === 'all'
        ? ''
        : this.view

    const token = ++this.pending
    try {
      const query = layer ? `&layer=${encodeURIComponent(layer)}` : ''
      const collection = await apiGet<TrailCollection>(
        `/api/trails?bbox=${bbox}${query}`,
      )
      // A slower earlier request must not overwrite a newer one.
      if (token !== this.pending) return

      this.setData(collection)
      this.restyle(collection.features.length)
      this.onStatus(
        collection.truncated
          ? `Showing the first ${collection.cap} tracks here. Zoom in for the rest.`
          : '',
      )
    } catch {
      if (token === this.pending) this.setData(EMPTY)
    }
  }
}
