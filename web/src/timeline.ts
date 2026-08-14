// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The year slider. Every stop is a view that was pre-rendered at ingest, so
// stepping through time is a change of raster URL and nothing more - no
// server-side render, no query, just tiles that already exist.

import { apiGet } from './api'

const VIEW_KEY = 'fogmap.view'
const PREHISTORY = 'prehistory'

export interface Stop {
  view: string
  /** Spelled out under the slider. */
  label: string
  /** The short mark on the tick strip, two or three characters at most. */
  tick: string
}

interface Meta {
  views: string[]
  layers: { layer: string; blobs: number }[]
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) throw new Error(`FogMap timeline is missing the element #${id}.`)
  return found as T
}

/**
 * Order the views into slider stops.
 *
 * Cumulative first, because that is the map people want to look at, then
 * anything undated, then the years in order.
 */
export function stopsFor(views: string[]): Stop[] {
  const stops: Stop[] = [{ view: 'all', label: 'All time', tick: '∑' }]

  if (views.includes(PREHISTORY)) {
    stops.push({ view: PREHISTORY, label: 'Before records', tick: 'pre' })
  }

  const years = views
    .filter((view) => view.startsWith('year:'))
    .map((view) => view.slice('year:'.length))
    .sort()

  for (const year of years) {
    stops.push({ view: `year:${year}`, label: year, tick: year.slice(-2) })
  }
  return stops
}

export class Timeline {
  private stops: Stop[] = [{ view: 'all', label: 'All time', tick: '∑' }]
  private index = 0
  private wired = false
  private readonly onChange: (view: string) => void

  constructor(onChange: (view: string) => void) {
    this.onChange = onChange
  }

  get view(): string {
    return this.stops[this.index]?.view ?? 'all'
  }

  /** Read the remembered view before the map is built, so it starts there. */
  static remembered(): string {
    try {
      return window.localStorage.getItem(VIEW_KEY) ?? 'all'
    } catch {
      return 'all'
    }
  }

  private remember(view: string): void {
    try {
      window.localStorage.setItem(VIEW_KEY, view)
    } catch {
      /* nothing to do */
    }
  }

  async load(): Promise<void> {
    let meta: Meta
    try {
      meta = await apiGet<Meta>('/api/meta')
    } catch {
      element('timeline').hidden = true
      return
    }

    this.stops = stopsFor(meta.views)

    // With nothing but the cumulative view there is no time to step through.
    if (this.stops.length < 2) {
      element('timeline').hidden = true
      return
    }

    const remembered = Timeline.remembered()
    const found = this.stops.findIndex((stop) => stop.view === remembered)
    this.index = found >= 0 ? found : 0

    this.build()
    element('timeline').hidden = false
    this.paint()
  }

  private build(): void {
    const slider = element<HTMLInputElement>('timeline-slider')
    slider.min = '0'
    slider.max = String(this.stops.length - 1)
    slider.step = '1'
    slider.value = String(this.index)

    const ticks = element('timeline-ticks')
    ticks.replaceChildren()
    for (const stop of this.stops) {
      const tick = document.createElement('span')
      tick.textContent = stop.tick
      tick.title = stop.label
      ticks.append(tick)
    }

    // load() runs again when a basemap arrives, so the listeners are attached
    // once and only once. Attaching them per load would make one click step
    // two years.
    if (this.wired) return
    this.wired = true

    slider.addEventListener('input', () => {
      this.go(Number(slider.value))
    })
    element('timeline-prev').addEventListener('click', () => this.go(this.index - 1))
    element('timeline-next').addEventListener('click', () => this.go(this.index + 1))
  }

  private go(next: number): void {
    const clamped = Math.max(0, Math.min(this.stops.length - 1, next))
    if (clamped === this.index) return

    this.index = clamped
    element<HTMLInputElement>('timeline-slider').value = String(clamped)
    this.paint()
    this.remember(this.view)
    this.onChange(this.view)
  }

  private paint(): void {
    element('timeline-label').textContent = this.stops[this.index].label

    const ticks = element('timeline-ticks').children
    for (let position = 0; position < ticks.length; position += 1) {
      ;(ticks[position] as HTMLElement).dataset.active = String(position === this.index)
    }

    element<HTMLButtonElement>('timeline-prev').disabled = this.index === 0
    element<HTMLButtonElement>('timeline-next').disabled =
      this.index === this.stops.length - 1
  }
}
