// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Search, which for now means: paste a coordinate and go there.
//
// The magnifying glass sits beside the settings button and opens a bar across
// the map. Parsing happens on the server - one implementation, tested next to
// everything else, and the same endpoint that will answer with pins and tracks
// when that part is built. Nothing about a search leaves the machine, which is
// the reason an external geocoder was argued against: every query would tell
// somebody else where you were looking.
//
// A found coordinate drops a marker rather than only moving the map. Arriving
// somewhere with nothing to show for it leaves you guessing which pixel was
// meant, and the marker is dashed and hollow so it never looks like a pin that
// has been saved. Saving it is one click, and until then it costs nothing: no
// event, no render, no row.

import { Marker, Popup } from 'maplibre-gl'
import type { Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend } from './api'
import { element } from './ui'

/** Close enough to read a street, without throwing away a closer view. */
const ARRIVAL_ZOOM = 15

/**
 * How long the flight takes.
 *
 * Capped because the default is derived from the distance, and a search from a
 * world view to a street is the longest journey the map can make - several
 * seconds of watching continents slide past before arriving somewhere you
 * already named. Long enough to see where it went, short enough not to wait.
 */
const FLIGHT_MS = 1400

interface Found {
  kind: string
  label: string
  detail: string
  lat: number
  lon: number
}

interface Answer {
  query: string
  results: Found[]
  hint: string
}

export class Search {
  private readonly map: MapLibreMap
  private readonly onSaved: () => void
  private pin: Marker | undefined

  constructor(map: MapLibreMap, onSaved: () => void) {
    this.map = map
    this.onSaved = onSaved
  }

  wire(): void {
    const bar = element<HTMLFormElement>('search-bar')
    const toggle = element('search-toggle')
    const input = element<HTMLInputElement>('search-input')

    toggle.addEventListener('click', () => {
      const opening = bar.hidden
      bar.hidden = !opening
      toggle.setAttribute('aria-pressed', String(opening))
      if (opening) input.focus()
      else this.reset()
    })

    bar.addEventListener('submit', (event) => {
      event.preventDefault()
      void this.run(input.value)
    })

    // Escape closes it, which is what every search box on every machine does.
    input.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape') return
      bar.hidden = true
      toggle.setAttribute('aria-pressed', 'false')
      this.reset()
    })
  }

  private async run(query: string): Promise<void> {
    if (!query.trim()) {
      this.say('')
      return
    }

    let answer: Answer
    try {
      answer = await apiGet<Answer>(
        `/api/search?q=${encodeURIComponent(query)}`,
        { timeoutMs: 15000 },
      )
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    const found = answer.results[0]
    if (!found) {
      this.say(answer.hint || 'Nothing found.', true)
      return
    }

    this.say(`${found.detail}: ${found.label}`)
    this.go(found)
  }

  private go(found: Found): void {
    this.drop(found)
    this.map.flyTo({
      center: [found.lon, found.lat],
      // Never zooms out. Somebody searching from street level wants that
      // street, not a view of the region it is in.
      zoom: Math.max(this.map.getZoom(), ARRIVAL_ZOOM),
      duration: FLIGHT_MS,
    })
  }

  /** The marker, and the offer to keep it. */
  private drop(found: Found): void {
    this.clearPin()

    const element_ = document.createElement('div')
    element_.className = 'search-pin'
    element_.title = found.label

    this.pin = new Marker({ element: element_, anchor: 'bottom' })
      .setLngLat([found.lon, found.lat])
      .setPopup(new Popup({ offset: 14, closeButton: false }).setDOMContent(
        this.offer(found),
      ))
      .addTo(this.map)

    this.pin.togglePopup()
  }

  /** What the temporary marker offers: a name, keep, or discard. */
  private offer(found: Found): HTMLElement {
    const box = document.createElement('div')
    box.className = 'search-offer'

    const where = document.createElement('p')
    where.className = 'hint'
    where.textContent = found.label
    box.append(where)

    const name = document.createElement('input')
    name.type = 'text'
    name.placeholder = 'Name this place'
    name.setAttribute('aria-label', 'Name for the new pin')
    box.append(name)

    const row = document.createElement('div')
    row.className = 'button-row'

    const keep = document.createElement('button')
    keep.type = 'button'
    keep.className = 'primary'
    keep.textContent = 'Save as pin'
    keep.addEventListener('click', () => void this.keep(found, name.value, keep))

    const drop = document.createElement('button')
    drop.type = 'button'
    drop.textContent = 'Discard'
    drop.addEventListener('click', () => {
      this.clearPin()
      this.say('')
    })

    row.append(keep, drop)
    box.append(row)

    // Enter in the name field means save, which is what it looks like it means.
    name.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        void this.keep(found, name.value, keep)
      }
    })

    return box
  }

  private async keep(found: Found, name: string, button: HTMLButtonElement): Promise<void> {
    const title = name.trim() || found.label
    button.disabled = true
    button.textContent = 'Saving…'
    try {
      await apiSend('POST', '/api/places', {
        name: title,
        lat: found.lat,
        lon: found.lon,
      })
      this.clearPin()
      this.say(`Saved ${title}.`)
      this.onSaved()
    } catch (error) {
      button.disabled = false
      button.textContent = 'Save as pin'
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
  }

  private clearPin(): void {
    this.pin?.remove()
    this.pin = undefined
  }

  private reset(): void {
    this.clearPin()
    this.say('')
  }

  private say(message: string, bad = false): void {
    const line = element('search-hint')
    line.textContent = message
    line.dataset.state = bad ? 'bad' : ''
  }
}
