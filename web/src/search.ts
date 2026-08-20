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

import { ApiError, apiGet, apiSend, getToken } from './api'
import { element } from './ui'

/** Close enough to read a street, without throwing away a closer view. */
const ARRIVAL_ZOOM = 15

/**
 * How long to wait after the last keystroke before asking.
 *
 * Short enough to feel like it is keeping up, long enough that typing a word
 * is one request rather than one per letter.
 */
const TYPING_PAUSE_MS = 180

/**
 * Below this, suggestions are noise: one letter matches most of an archive, and
 * the answer arrives too late to be about what is on screen anyway.
 */
const MIN_QUERY = 2

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
  /** Present on a track: a track is a shape, so the map frames all of it. */
  bounds?: [[number, number], [number, number]]
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
  private matches: Found[] = []
  private active = 0
  private searched = ''
  private typing: number | undefined
  //: Which request is the current one. Answers can come back out of order, and
  //: a slow reply to "cao" must not overwrite the results for "caorle".
  private asked = 0

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

      // Enter on a list somebody has arrowed through means "that one", not
      // "search again" - otherwise the highlight is decoration.
      const chosen = this.matches[this.active]
      if (chosen && input.value === this.searched) {
        this.go(chosen)
        return
      }
      void this.run(input.value)
    })

    // Suggest as it is typed. Suggest, not travel: an intermediate coordinate
    // parses perfectly well - "27.74367, -1" is a real place - and flying there
    // on the way to somewhere else is worse than not moving at all.
    input.addEventListener('input', () => {
      if (this.typing !== undefined) window.clearTimeout(this.typing)
      const text = input.value
      this.typing = window.setTimeout(() => {
        this.typing = undefined
        void this.suggest(text)
      }, TYPING_PAUSE_MS)
    })

    input.addEventListener('keydown', (event) => {
      // Escape closes it, which is what every search box on every machine does.
      if (event.key === 'Escape') {
        bar.hidden = true
        toggle.setAttribute('aria-pressed', 'false')
        this.reset()
        return
      }

      // Arrows walk the matches. A list you can only click is a list that
      // makes you take your hands off the keys mid-search.
      if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
      if (!this.matches.length) return
      event.preventDefault()
      const step = event.key === 'ArrowDown' ? 1 : -1
      this.active =
        (this.active + step + this.matches.length) % this.matches.length
      this.paintResults()
    })
  }

  /** Look, and show what there is. Does not move the map. */
  private async suggest(query: string): Promise<Answer | null> {
    const text = query.trim()
    if (text.length < MIN_QUERY) {
      this.matches = []
      this.searched = ''
      this.paintResults()
      this.say('')
      return null
    }

    const mine = ++this.asked
    let answer: Answer
    try {
      answer = await apiGet<Answer>(
        `/api/search?q=${encodeURIComponent(query)}`,
        { timeoutMs: 15000 },
      )
    } catch (error) {
      if (mine !== this.asked) return null
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return null
    }

    // A newer keystroke has already asked, so this answer is about a query
    // nobody is looking at any more.
    if (mine !== this.asked) return null

    this.matches = answer.results
    this.active = 0
    this.searched = query
    this.paintResults()

    if (!answer.results.length) {
      this.say(answer.hint || 'Nothing found.', true)
      return answer
    }

    this.say(
      answer.hint ||
        (answer.results.length === 1
          ? `${answer.results[0].detail}: ${answer.results[0].label}`
          : `${answer.results.length} matches — Enter to take the first.`),
    )
    return answer
  }

  /** Look, and go to the best answer. What pressing Go or Enter means. */
  private async run(query: string): Promise<void> {
    if (!query.trim()) {
      this.say('')
      return
    }

    const answer = await this.suggest(query)
    const found = answer?.results[0]
    if (found) this.go(found)
  }

  /** The matches, when there is more than one to choose between. */
  private paintResults(): void {
    const list = element('search-results')
    list.replaceChildren()
    // Shown for a single match as well, now that finding something no longer
    // moves the map by itself: the list is the thing that says what was found.
    list.hidden = this.matches.length === 0
    if (list.hidden) return

    this.matches.forEach((found, index) => {
      const row = document.createElement('li')
      row.dataset.active = String(index === this.active)

      const choose = document.createElement('button')
      choose.type = 'button'

      const name = document.createElement('span')
      name.textContent = found.label
      const detail = document.createElement('span')
      detail.className = 'result-detail'
      detail.textContent = found.detail

      choose.append(name, detail)
      choose.addEventListener('click', () => {
        this.active = index
        this.paintResults()
        this.go(found)
      })

      row.append(choose)
      list.append(row)
    })
  }

  private go(found: Found): void {
    // A track gets framed rather than flown to: its middle is often nowhere
    // near any of it, and arriving at the centre of a 400 km ride shows a field.
    if (found.bounds) {
      this.clearPin()
      this.map.fitBounds(found.bounds, { padding: 60, duration: FLIGHT_MS })
      return
    }

    // A pin is already on the map. Only a coordinate needs one dropping, and
    // only a coordinate is worth offering to keep.
    if (found.kind === 'coordinates') this.drop(found)
    else this.clearPin()

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

    // Saving is a write, and searching is not. Without a token the coordinate
    // is still found, still flown to and still marked - what goes away is the
    // offer to keep it, rather than a button that fails when pressed.
    if (!getToken()) {
      const locked = document.createElement('p')
      locked.className = 'hint'
      locked.textContent =
        'Add the API token under Settings, Security to keep this as a pin.'
      box.append(locked)
      return box
    }

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
    if (this.typing !== undefined) window.clearTimeout(this.typing)
    this.typing = undefined
    this.clearPin()
    this.matches = []
    this.active = 0
    this.searched = ''
    this.paintResults()
    this.say('')
  }

  private say(message: string, bad = false): void {
    const line = element('search-hint')
    line.textContent = message
    line.dataset.state = bad ? 'bad' : ''
  }
}
