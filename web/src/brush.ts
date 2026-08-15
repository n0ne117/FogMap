// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the brush looks like before it is committed. Two things live here: the
// ring under the cursor showing how wide the brush is, and the preview of the
// stroke in progress.
//
// Both are drawn at the true ground width. A brush described only as "15 m" is
// a number nobody can act on - the useful question is whether it covers that
// side street, and the only answer is to show it on the map at the size it
// will land.

import type { Map as MapLibreMap } from 'maplibre-gl'

import type { Op, Point, Tool } from './draw'

const SOURCE = 'fogmap-draw-preview'
const AREA_LAYER = 'fogmap-draw-area'
const SWATH_LAYER = 'fogmap-draw-swath'
const SPINE_LAYER = 'fogmap-draw-spine'

/** Ground covered by one screen pixel at zoom 0 on the equator. */
const M_PER_PX_Z0 = 156_543.03392

const REVEAL = '#ffffff'
const ERASE = '#e0645c'

const EMPTY = { type: 'FeatureCollection', features: [] } as const

/** Metres per screen pixel at a zoom and latitude. */
export function metresPerPixel(zoom: number, latitude: number): number {
  return (M_PER_PX_Z0 * Math.cos((latitude * Math.PI) / 180)) / 2 ** zoom
}

export class Brush {
  private readonly map: MapLibreMap
  private readonly ring: HTMLElement
  private tool: Tool = 'off'
  private points: Point[] = []

  radiusM = 15

  constructor(map: MapLibreMap, ring: HTMLElement) {
    this.map = map
    this.ring = ring

    // The ring is sized in screen pixels, so it has to be resized whenever the
    // ground under it changes scale.
    map.on('zoom', () => this.resize())
    map.on('move', () => this.resize())
  }

  private get op(): Op {
    return this.tool === 'eraser' ? 'erase' : this.tool === 'off' ? 'add' : 'reveal'
  }

  private get closes(): boolean {
    return this.tool === 'area'
  }

  private get diameterPx(): number {
    const metres = metresPerPixel(this.map.getZoom(), this.map.getCenter().lat)
    return (this.radiusM * 2) / metres
  }

  /**
   * Add the preview source and layers. Safe to call more than once, and has
   * to be called again after a style change, which drops both.
   *
   * Deliberately not guarded on isStyleLoaded(). That reports whether every
   * source has finished loading, not whether the style is usable - so the
   * trail layer attaching immediately before this one flipped it to false and
   * the preview was never added at all. The caller attaches on style.load,
   * which is the condition that actually matters.
   */
  attach(): void {
    if (this.map.getSource(SOURCE)) return

    this.map.addSource(SOURCE, { type: 'geojson', data: EMPTY as never })

    // The area tool encloses ground rather than covering a line, so what
    // matters while drawing is what is inside the ring.
    this.map.addLayer({
      id: AREA_LAYER,
      type: 'fill',
      source: SOURCE,
      filter: ['==', ['geometry-type'], 'Polygon'],
      paint: { 'fill-color': REVEAL, 'fill-opacity': 0.18 },
    })

    // The swath is the brush itself, at ground width. The spine is a hairline
    // down the middle: at a 15 m brush zoomed out the swath alone is too faint
    // to tell exactly where the stroke went.
    this.map.addLayer({
      id: SWATH_LAYER,
      type: 'line',
      source: SOURCE,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: {
        'line-color': REVEAL,
        'line-width': this.diameterPx,
        'line-opacity': 0.28,
      },
    })
    this.map.addLayer({
      id: SPINE_LAYER,
      type: 'line',
      source: SOURCE,
      layout: { 'line-join': 'round', 'line-cap': 'round' },
      paint: { 'line-color': REVEAL, 'line-width': 1.5, 'line-opacity': 0.9 },
    })

    this.repaint()
  }

  /** Follow the tool the toolbar has selected. */
  setTool(tool: Tool): void {
    this.tool = tool
    // The area tool encloses ground; the brush footprint is not what governs
    // it, so showing a ring for it would be a lie about what will be covered.
    this.ring.hidden = tool === 'off' || tool === 'area'
    this.repaint()
    this.resize()
  }

  setRadius(radiusM: number): void {
    this.radiusM = radiusM
    this.resize()
  }

  /** Move the ring to the pointer. */
  track(event: PointerEvent | MouseEvent): void {
    if (this.tool === 'off' || this.closes) return
    this.ring.hidden = false
    this.ring.style.left = `${event.clientX}px`
    this.ring.style.top = `${event.clientY}px`
  }

  hideRing(): void {
    this.ring.hidden = true
  }

  /** Show the stroke as it is being drawn. */
  preview(points: Point[]): void {
    this.points = points
    if (points.length === 0) {
      this.setData(EMPTY)
      return
    }

    const at = (point: Point) => [point.lng, point.lat]
    // A single point is not a line. Doubling it gives the round cap something
    // to draw, so a dab shows as a dot.
    const line = (points.length === 1 ? [points[0], points[0]] : points).map(at)
    const features: unknown[] = [
      { type: 'Feature', properties: {}, geometry: { type: 'LineString', coordinates: line } },
    ]

    // The ring only becomes a shape at three corners; before that the line is
    // the honest thing to show.
    if (this.closes && points.length >= 3) {
      features.push({
        type: 'Feature',
        properties: {},
        geometry: { type: 'Polygon', coordinates: [[...points, points[0]].map(at)] },
      })
    }

    this.setData({ type: 'FeatureCollection', features })
  }

  clear(): void {
    this.points = []
    this.setData(EMPTY)
  }

  private setData(collection: unknown): void {
    const source = this.map.getSource(SOURCE)
    if (source && 'setData' in source) {
      ;(source as { setData: (data: unknown) => void }).setData(collection)
    }
  }

  private repaint(): void {
    if (!this.map.getLayer(SWATH_LAYER)) return
    const colour = this.op === 'erase' ? ERASE : REVEAL
    this.map.setPaintProperty(SWATH_LAYER, 'line-color', colour)
    this.map.setPaintProperty(SPINE_LAYER, 'line-color', colour)
    this.map.setPaintProperty(AREA_LAYER, 'fill-color', colour)
    this.ring.dataset.mode = this.op === 'erase' ? 'erase' : 'add'
  }

  private resize(): void {
    const diameter = this.diameterPx
    this.ring.style.width = `${diameter}px`
    this.ring.style.height = `${diameter}px`

    if (this.map.getLayer(SWATH_LAYER)) {
      this.map.setPaintProperty(SWATH_LAYER, 'line-width', diameter)
    }
    // The stroke keeps its ground width as the map moves under it.
    if (this.points.length) this.preview(this.points)
  }
}
