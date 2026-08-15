// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Manual editing. A stroke becomes a GeoJSON LineString and is posted to
// /api/events, which puts it through exactly the path a GPX import takes.
// Drawing is not a special case anywhere below this file.

import { ApiError, apiGet, apiSend } from './api'

/** Below this the brush is meaningless: one screen pixel exceeds its diameter. */
export const MIN_DRAW_ZOOM = 14

/** Points closer together than this add nothing but bytes. */
const THIN_METRES = 5

/**
 * Corner-cutting passes run over a freehand stroke before it is posted.
 *
 * A pointer trace is a staircase of screen pixels, and thinning it to five
 * metres leaves the staircase, just with longer steps. Two Chaikin passes turn
 * the corners into curves - four times the points, still under a hundred for a
 * normal stroke - which is what "smooth" has to mean here: the geometry is
 * smooth, so it is still smooth at z18 and after a rebuild. Smoothing only the
 * drawn line would leave the fog underneath it faceted.
 */
const SMOOTH_PASSES = 2

export type Tool = 'off' | 'freehand' | 'line' | 'eraser'
export type Mode = 'add' | 'erase'

export interface Point {
  lng: number
  lat: number
}

interface MapLike {
  getZoom(): number
  getCanvas(): HTMLCanvasElement
  on(event: string, handler: (event: never) => void): unknown
  unproject(point: [number, number]): Point
  dragPan: { enable(): void; disable(): void }
}

export function metresBetween(a: Point, b: Point): number {
  const earth = 6_371_008.8
  const toRad = Math.PI / 180
  const dLat = (b.lat - a.lat) * toRad
  const dLng = (b.lng - a.lng) * toRad
  const midLat = ((a.lat + b.lat) / 2) * toRad
  const x = dLng * Math.cos(midLat)
  return Math.hypot(x, dLat) * earth
}

/** Drop points closer than the threshold to the last one kept. */
export function thinByDistance(points: Point[], metres = THIN_METRES): Point[] {
  if (points.length < 2) return points.slice()

  const kept = [points[0]]
  for (const point of points.slice(1, -1)) {
    if (metresBetween(kept[kept.length - 1], point) >= metres) {
      kept.push(point)
    }
  }
  kept.push(points[points.length - 1])
  return kept
}

/** Douglas-Peucker, so a straight run collapses to its endpoints. */
export function simplify(points: Point[], tolerance = THIN_METRES): Point[] {
  if (points.length < 3) return points.slice()

  const first = points[0]
  const last = points[points.length - 1]

  let worst = 0
  let index = 0
  for (let i = 1; i < points.length - 1; i += 1) {
    const distance = perpendicularMetres(points[i], first, last)
    if (distance > worst) {
      worst = distance
      index = i
    }
  }

  if (worst <= tolerance) return [first, last]

  const left = simplify(points.slice(0, index + 1), tolerance)
  const right = simplify(points.slice(index), tolerance)
  return [...left.slice(0, -1), ...right]
}

/**
 * Chaikin corner cutting. Each pass replaces every span with its quarter and
 * three-quarter points, so the polyline converges on a quadratic B-spline.
 *
 * The endpoints are kept exactly. A stroke that ends where the user lifted the
 * pointer is the one thing about a freehand line they aimed at.
 */
export function smooth(points: Point[], passes = SMOOTH_PASSES): Point[] {
  let current = points.slice()

  for (let pass = 0; pass < passes; pass += 1) {
    if (current.length < 3) return current

    const next: Point[] = [current[0]]
    for (let i = 0; i < current.length - 1; i += 1) {
      const a = current[i]
      const b = current[i + 1]
      next.push({ lng: a.lng * 0.75 + b.lng * 0.25, lat: a.lat * 0.75 + b.lat * 0.25 })
      next.push({ lng: a.lng * 0.25 + b.lng * 0.75, lat: a.lat * 0.25 + b.lat * 0.75 })
    }
    next.push(current[current.length - 1])
    current = next
  }

  return current
}

function perpendicularMetres(point: Point, start: Point, end: Point): number {
  const spanLength = metresBetween(start, end)
  if (spanLength === 0) return metresBetween(point, start)

  // Work in a local flat frame; over a brush stroke the curvature is nil.
  const toRad = Math.PI / 180
  const scale = Math.cos(start.lat * toRad)
  const ax = (point.lng - start.lng) * scale
  const ay = point.lat - start.lat
  const bx = (end.lng - start.lng) * scale
  const by = end.lat - start.lat

  const cross = Math.abs(ax * by - ay * bx)
  const magnitude = Math.hypot(bx, by)
  return magnitude === 0 ? 0 : (cross / magnitude) * 111_320
}

export interface DrawResult {
  id: number
  op: Mode
  layers: string[]
}

export class Draw {
  private tool: Tool = 'off'
  private drawing = false
  private undoing = false
  private points: Point[] = []
  private readonly undoStack: number[] = []
  private readonly map: MapLike
  private readonly onSaved: () => void
  private readonly onStatus: (message: string, bad?: boolean) => void

  layers = ''
  radiusM = 15

  /**
   * Called with the stroke so far on every change, and with nothing once it
   * has been sent. Drawing blind and finding out what you drew a second later
   * when the tiles come back is not drawing.
   */
  onPreview: (points: Point[]) => void = () => {}

  constructor(map: MapLike, onSaved: () => void, onStatus: (m: string, bad?: boolean) => void) {
    this.map = map
    this.onSaved = onSaved
    this.onStatus = onStatus
  }

  get activeTool(): Tool {
    return this.tool
  }

  /**
   * What a stroke with the current tool does.
   *
   * The eraser is a tool rather than a mode next to the brush. A control that
   * says "Erase" and sits away from the tool it modifies reads as a button
   * that erases something, which is the last thing a drawing app should be
   * ambiguous about.
   */
  get mode(): Mode {
    return this.tool === 'eraser' ? 'erase' : 'add'
  }

  get canDraw(): boolean {
    return this.map.getZoom() >= MIN_DRAW_ZOOM
  }

  get undoDepth(): number {
    return this.undoStack.length
  }

  get busy(): boolean {
    return this.undoing
  }

  setTool(tool: Tool): void {
    this.tool = tool
    this.points = []
    this.drawing = false
    this.onPreview([])
    // 'off' is the hand tool: the map pans and nothing is painted.
    this.map.getCanvas().style.cursor =
      tool === 'off' ? 'grab' : tool === 'eraser' ? 'cell' : 'crosshair'
    if (tool === 'off') {
      this.map.dragPan.enable()
    }
  }

  attach(): void {
    const canvas = this.map.getCanvas()

    canvas.addEventListener('pointerdown', (event) => this.begin(event))
    canvas.addEventListener('pointermove', (event) => this.extend(event))
    canvas.addEventListener('pointerup', () => void this.finish())
    canvas.addEventListener('pointerleave', () => void this.finish())

    // A point-to-point line is finished by double click rather than release.
    canvas.addEventListener('dblclick', (event) => {
      if (this.tool === 'line' && this.drawing) {
        event.preventDefault()
        void this.finish(true)
      }
    })
  }

  private at(event: PointerEvent | MouseEvent): Point {
    const rect = this.map.getCanvas().getBoundingClientRect()
    return this.map.unproject([event.clientX - rect.left, event.clientY - rect.top])
  }

  private begin(event: PointerEvent): void {
    if (this.tool === 'off' || !this.canDraw) return
    event.preventDefault()

    this.map.dragPan.disable()

    if (this.tool === 'line') {
      // Each click adds a vertex; the stroke ends on double click.
      if (!this.drawing) {
        this.drawing = true
        this.points = [this.at(event)]
      } else {
        this.points.push(this.at(event))
      }
      this.onStatus(`${this.points.length} points, double click to finish`)
      this.onPreview(this.points)
      return
    }

    this.drawing = true
    this.points = [this.at(event)]
    this.onPreview(this.points)
  }

  private extend(event: PointerEvent): void {
    if (!this.drawing) return

    // The line tool rubber-bands to the cursor rather than recording it: the
    // segment being aimed is the one worth seeing.
    if (this.tool === 'line') {
      this.onPreview([...this.points, this.at(event)])
      return
    }

    this.points.push(this.at(event))
    this.onPreview(this.points)
  }

  private async finish(force = false): Promise<void> {
    if (!this.drawing) return
    if (this.tool === 'line' && !force) return

    this.drawing = false
    this.map.dragPan.enable()

    const raw = this.points
    this.points = []

    if (raw.length === 0) {
      this.onPreview([])
      return
    }
    if (raw.length === 1 && this.tool === 'line') {
      this.onStatus('A line needs at least two points.')
      this.onPreview([])
      return
    }

    // A point-to-point line is meant to be straight, so it is left alone.
    const thinned =
      this.tool === 'line'
        ? raw
        : smooth(simplify(thinByDistance(raw), THIN_METRES))

    // The preview holds - now showing the smoothed line that is actually
    // being saved - until the rebuilt tiles arrive, so the stroke does not
    // blink out of existence while the server works.
    this.onPreview(thinned)

    const geometry =
      thinned.length === 1
        ? { type: 'Point', coordinates: [thinned[0].lng, thinned[0].lat] }
        : {
            type: 'LineString',
            coordinates: thinned.map((p) => [p.lng, p.lat]),
          }

    const mode = this.mode
    try {
      const saved = await apiSend<DrawResult>('POST', '/api/events', {
        source: 'manual',
        op: mode,
        geometry,
        radius_m: this.radiusM,
        layers: mode === 'erase' ? undefined : this.layerList(),
      })
      this.undoStack.push(saved.id)
      this.onStatus(
        `${mode === 'erase' ? 'Erased' : 'Drew'} ${thinned.length} points ` +
          `into ${saved.layers.join(', ')}`,
      )
      this.onSaved()
    } catch (error) {
      const message = error instanceof ApiError ? error.message : String(error)
      this.onStatus(message, true)
    } finally {
      this.onPreview([])
    }
  }

  private layerList(): string[] | undefined {
    const trimmed = this.layers.trim()
    return trimmed ? trimmed.split(/[,\s]+/).filter(Boolean) : undefined
  }

  /**
   * Remove the most recent hand-drawn stroke.
   *
   * The stack of ids this session drew is only a fast path. When it is empty
   * the most recent manual event is fetched instead, so undo still works after
   * a reload rather than claiming there is nothing to undo while the stroke is
   * plainly on screen.
   */
  async undo(): Promise<void> {
    if (this.undoing) return

    this.undoing = true
    this.onStatus('Undoing the last stroke.')
    let id = this.undoStack.pop()

    try {
      if (id === undefined) id = await lastManualEvent()
      if (id === undefined) {
        this.onStatus('Nothing to undo. Only hand-drawn strokes can be undone.')
        return
      }

      await apiSend('DELETE', `/api/events/${id}`)
      this.onStatus(`Undid stroke ${id}`)
    } catch (error) {
      const message = error instanceof ApiError ? error.message : String(error)
      this.onStatus(message, true)
      if (id !== undefined) this.undoStack.push(id)
    } finally {
      this.undoing = false
      this.onSaved()
    }
  }
}

interface EventList {
  events: { id: number }[]
}

/** The newest manual event, or undefined if nothing has been drawn by hand. */
async function lastManualEvent(): Promise<number | undefined> {
  const list = await apiGet<EventList>('/api/events?source=manual&limit=1')
  return list.events[0]?.id
}
