// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Named places. A place is a marker you can read and an event that clears the
// fog around it - the second half happens on the server, through the same
// path as everything else.

import { Marker, Popup } from 'maplibre-gl'
import type { Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend } from './api'
import { element } from './ui'

export interface Place {
  id: number
  name: string
  category: string | null
  people: string[]
  date_from: string | null
  date_to: string | null
  lat: number
  lon: number
  event_id: number | null
}

interface PlacesResponse {
  places: Place[]
  people: string[]
  categories: string[]
}

function escapeHtml(value: string): string {
  const div = document.createElement('div')
  div.textContent = value
  return div.innerHTML
}

/** What a marker shows when clicked. */
export function popupHtml(place: Place): string {
  const rows: string[] = [`<strong>${escapeHtml(place.name)}</strong>`]

  if (place.category) {
    rows.push(`<span class="pill">${escapeHtml(place.category)}</span>`)
  }

  const dates = [place.date_from, place.date_to].filter(Boolean).map(String)
  if (dates.length) {
    rows.push(`<div class="place-dates">${escapeHtml(dates.join(' to '))}</div>`)
  }

  if (place.people.length) {
    rows.push(
      `<div class="place-people">${escapeHtml(place.people.join(', '))}</div>`,
    )
  }

  return `<div class="place-popup">${rows.join('')}</div>`
}

export class Places {
  private readonly map: MapLibreMap
  private readonly onChanged: () => void
  private markers: Marker[] = []
  private all: Place[] = []
  private person = ''

  constructor(map: MapLibreMap, onChanged: () => void) {
    this.map = map
    this.onChanged = onChanged
  }

  get count(): number {
    return this.all.length
  }

  get visible(): Place[] {
    if (!this.person) return this.all
    return this.all.filter((place) =>
      place.people.some(
        (name) => name.toLowerCase() === this.person.toLowerCase(),
      ),
    )
  }

  async load(): Promise<void> {
    let body: PlacesResponse
    try {
      body = await apiGet<PlacesResponse>('/api/places')
    } catch {
      return
    }

    this.all = body.places
    this.fillPeople(body.people)
    this.fillCategories(body.categories)
    this.draw()
    this.list()
  }

  private fillPeople(people: string[]): void {
    const select = element<HTMLSelectElement>('place-person')
    const current = select.value

    select.replaceChildren()
    const everyone = document.createElement('option')
    everyone.value = ''
    everyone.textContent = `Everyone (${this.all.length})`
    select.append(everyone)

    for (const name of people) {
      const option = document.createElement('option')
      option.value = name
      option.textContent = name
      select.append(option)
    }
    select.value = people.includes(current) ? current : ''
    this.person = select.value
    element('place-filter-row').hidden = people.length === 0
  }

  private fillCategories(categories: string[]): void {
    const select = element<HTMLSelectElement>('place-category')
    if (select.options.length > 0) return

    const none = document.createElement('option')
    none.value = ''
    none.textContent = 'none'
    select.append(none)

    for (const category of categories) {
      const option = document.createElement('option')
      option.value = category
      option.textContent = category
      select.append(option)
    }
  }

  /** Rebuild the markers from scratch; there are never enough to diff. */
  private draw(): void {
    for (const marker of this.markers) marker.remove()
    this.markers = []

    for (const place of this.visible) {
      const marker = new Marker({ color: '#d8a13a' })
        .setLngLat([place.lon, place.lat])
        .setPopup(new Popup({ offset: 18 }).setHTML(popupHtml(place)))
        .addTo(this.map)
      this.markers.push(marker)
    }
  }

  private list(): void {
    const box = element('place-list')
    box.replaceChildren()

    for (const place of this.visible) {
      const row = document.createElement('div')
      row.className = 'place-row'

      const go = document.createElement('button')
      go.type = 'button'
      go.className = 'place-name'
      go.textContent = place.name
      go.title = 'Show on the map'
      go.addEventListener('click', () => {
        this.map.flyTo({ center: [place.lon, place.lat], zoom: 15 })
      })

      const remove = document.createElement('button')
      remove.type = 'button'
      remove.className = 'place-delete'
      remove.textContent = '×'
      remove.title = `Delete ${place.name}`
      remove.addEventListener('click', () => void this.remove(place))

      row.append(go, remove)
      box.append(row)
    }

    element('place-empty').hidden = this.visible.length > 0
  }

  setPerson(person: string): void {
    this.person = person
    this.draw()
    this.list()
  }

  /** Prefill the form from wherever the map is looking. */
  openForm(at?: { lng: number; lat: number }): void {
    const centre = at ?? this.map.getCenter()
    element<HTMLInputElement>('place-lat').value = centre.lat.toFixed(6)
    element<HTMLInputElement>('place-lon').value = centre.lng.toFixed(6)
    element('place-form').hidden = false
    element<HTMLInputElement>('place-name').focus()
  }

  closeForm(): void {
    element('place-form').hidden = true
    for (const id of ['place-name', 'place-people', 'place-from', 'place-to']) {
      element<HTMLInputElement>(id).value = ''
    }
    this.say('')
  }

  private say(message: string, bad = false): void {
    const box = element('place-message')
    box.textContent = message
    box.hidden = !message
    box.dataset.state = bad ? 'bad' : ''
  }

  async save(): Promise<void> {
    const body = {
      name: element<HTMLInputElement>('place-name').value.trim(),
      category: element<HTMLSelectElement>('place-category').value || null,
      people: element<HTMLInputElement>('place-people').value,
      date_from: element<HTMLInputElement>('place-from').value.trim() || null,
      date_to: element<HTMLInputElement>('place-to').value.trim() || null,
      lat: Number(element<HTMLInputElement>('place-lat').value),
      lon: Number(element<HTMLInputElement>('place-lon').value),
    }

    try {
      await apiSend<Place>('POST', '/api/places', body)
      this.closeForm()
      await this.load()
      this.onChanged()
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
  }

  private async remove(place: Place): Promise<void> {
    try {
      await apiSend('DELETE', `/api/places/${place.id}`)
      await this.load()
      this.onChanged()
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
  }

  wire(): void {
    element('place-add').addEventListener('click', () => this.openForm())
    element('place-cancel').addEventListener('click', () => this.closeForm())
    element('place-save').addEventListener('click', () => void this.save())
    element<HTMLSelectElement>('place-person').addEventListener('change', (event) => {
      this.setPerson((event.target as HTMLSelectElement).value)
    })

    // While the form is open, clicking the map moves the pin rather than
    // making the user type coordinates.
    this.map.on('click', (event) => {
      if (element('place-form').hidden) return
      const point = event as unknown as { lngLat: { lng: number; lat: number } }
      element<HTMLInputElement>('place-lat').value = point.lngLat.lat.toFixed(6)
      element<HTMLInputElement>('place-lon').value = point.lngLat.lng.toFixed(6)
    })
  }
}
