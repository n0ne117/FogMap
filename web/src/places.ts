// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Places: pins on the map and the sidebar that organises them.
//
// A pin is two things at once. It is a row somebody can read - a title, a
// label, tags, a folder - and it is an event that clears the fog around it,
// which goes through the same path as everything else. Dropping a pin on the
// village you grew up in reveals it exactly as walking there would have.

import { Marker, Popup } from 'maplibre-gl'
import type { Map as MapLibreMap } from 'maplibre-gl'

import { ApiError, apiGet, apiSend } from './api'
import { element } from './ui'

/** Ground a dropped pin clears, in metres. */
export const PLACE_RADIUS_M = 30

const NO_LABEL_COLOUR = '#8a8f98'

export interface Label {
  id: number
  name: string
  colour: string
}

export interface Folder {
  id: number
  name: string
  parent_id: number | null
  visible: boolean
}

export interface Place {
  id: number
  name: string
  label_id: number | null
  folder_id: number | null
  tags: string[]
  lat: number
  lon: number
}

interface PlacesResponse {
  places: Place[]
  labels: Label[]
  folders: Folder[]
}

/** A pin being placed but not yet saved. */
interface Pending {
  lat: number
  lon: number
  marker: Marker
  editing: number | null
}

export class Places {
  private readonly map: MapLibreMap
  private readonly onChanged: () => void

  private places: Place[] = []
  private labels: Label[] = []
  private folders: Folder[] = []

  private markers = new Map<number, Marker>()
  private pending: Pending | null = null
  private dropping = false
  private collapsed = new Set<number>()

  constructor(map: MapLibreMap, onChanged: () => void) {
    this.map = map
    this.onChanged = onChanged
  }

  // ------------------------------------------------------------------ wiring

  wire(): void {
    element('place-drop').addEventListener('click', () => this.armDrop())
    element('folder-add').addEventListener('click', () => void this.newFolder())
    element('place-save').addEventListener('click', () => void this.save())
    element('place-cancel').addEventListener('click', () => this.cancel())

    this.map.on('click', (event) => {
      if (!this.dropping) return
      this.dropAt(event.lngLat.lat, event.lngLat.lng)
    })

    // Escape gets you out of drop mode without saving anything, which is the
    // one thing every modal state has to offer.
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape' && this.dropping) this.disarmDrop()
    })
  }

  async load(): Promise<void> {
    try {
      const body = await apiGet<PlacesResponse>('/api/places')
      this.places = body.places ?? []
      this.labels = body.labels ?? []
      this.folders = body.folders ?? []
      this.say('')
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    this.paintTree()
    this.paintMarkers()
    this.paintPickers()
  }

  // -------------------------------------------------------------- dropping

  private armDrop(): void {
    this.dropping = true
    element('map').dataset.dropping = 'true'
    this.say('Click the map to place the pin. Escape to stop.')
  }

  private disarmDrop(): void {
    this.dropping = false
    delete element('map').dataset.dropping
    this.say('')
  }

  private dropAt(lat: number, lon: number): void {
    this.disarmDrop()
    this.pending?.marker.remove()

    const marker = new Marker({ color: NO_LABEL_COLOUR, draggable: true })
      .setLngLat([lon, lat])
      .addTo(this.map)

    // Draggable, because nobody lands on the right pixel first time.
    marker.on('dragend', () => {
      const at = marker.getLngLat()
      if (this.pending) {
        this.pending.lat = at.lat
        this.pending.lon = at.lng
        this.showCoords()
      }
    })

    this.pending = { lat, lon, marker, editing: null }
    this.openForm('New pin')
  }

  private openForm(title: string): void {
    element('place-form-title').textContent = title
    element('place-form').hidden = false
    this.showCoords()
    element<HTMLInputElement>('place-name').focus()
  }

  private showCoords(): void {
    if (!this.pending) return
    element('place-coords').textContent =
      `${this.pending.lat.toFixed(6)}, ${this.pending.lon.toFixed(6)} — ` +
      `clears ${PLACE_RADIUS_M} m of fog`
  }

  private cancel(): void {
    this.pending?.marker.remove()
    this.pending = null
    element('place-form').hidden = true
    this.clearForm()
    this.say('')
  }

  private clearForm(): void {
    element<HTMLInputElement>('place-name').value = ''
    element<HTMLInputElement>('place-tags').value = ''
    element<HTMLSelectElement>('place-label').value = ''
    element<HTMLSelectElement>('place-folder').value = ''
  }

  private async save(): Promise<void> {
    if (!this.pending) return

    const name = element<HTMLInputElement>('place-name').value.trim()
    if (!name) {
      this.say('A pin needs a title.', true)
      return
    }

    const body = {
      name,
      lat: this.pending.lat,
      lon: this.pending.lon,
      label_id: element<HTMLSelectElement>('place-label').value || null,
      folder_id: element<HTMLSelectElement>('place-folder').value || null,
      tags: element<HTMLInputElement>('place-tags').value,
      radius_m: PLACE_RADIUS_M,
    }

    const editing = this.pending.editing
    this.say(editing ? 'Saving…' : 'Saving, and clearing the fog around it…')

    try {
      if (editing) {
        await apiSend('PATCH', `/api/places/${editing}`, body)
      } else {
        await apiSend('POST', '/api/places', body)
      }
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    this.pending.marker.remove()
    this.pending = null
    element('place-form').hidden = true
    this.clearForm()
    this.say('')

    await this.load()
    this.onChanged()
  }

  // ---------------------------------------------------------------- markers

  private labelOf(place: Place): Label | undefined {
    return this.labels.find((label) => label.id === place.label_id)
  }

  /** Is this place's folder, or any folder above it, switched off? */
  private isHidden(place: Place): boolean {
    let current = place.folder_id
    const walked = new Set<number>()
    while (current !== null && current !== undefined && !walked.has(current)) {
      walked.add(current)
      const folder = this.folders.find((item) => item.id === current)
      if (!folder) return false
      if (!folder.visible) return true
      current = folder.parent_id
    }
    return false
  }

  private paintMarkers(): void {
    for (const marker of this.markers.values()) marker.remove()
    this.markers.clear()

    for (const place of this.places) {
      if (this.isHidden(place)) continue

      const marker = new Marker({ color: this.labelOf(place)?.colour ?? NO_LABEL_COLOUR })
        .setLngLat([place.lon, place.lat])
        .setPopup(new Popup({ offset: 26 }).setDOMContent(this.popupFor(place)))
        .addTo(this.map)

      marker.getElement().classList.add('place-pin')
      this.markers.set(place.id, marker)
    }
  }

  /**
   * What a saved pin says when it is clicked.
   *
   * Built as DOM rather than a string of HTML: the buttons need handlers, and
   * a title someone typed goes in as text, so a place called `<script>` is a
   * place called `<script>`.
   */
  private popupFor(place: Place): HTMLElement {
    const root = document.createElement('div')
    root.className = 'place-popup'

    const heading = document.createElement('h3')
    heading.textContent = place.name
    root.append(heading)

    const label = this.labelOf(place)
    const folder = this.folders.find((item) => item.id === place.folder_id)
    const filed = [label?.name, folder?.name].filter(Boolean).join(' · ')
    if (filed) {
      const line = document.createElement('div')
      line.className = 'coords'
      line.textContent = filed
      root.append(line)
    }

    const coords = document.createElement('div')
    coords.className = 'coords'
    coords.textContent = `${place.lat.toFixed(6)}, ${place.lon.toFixed(6)}`
    root.append(coords)

    if (place.tags.length) {
      const tags = document.createElement('div')
      tags.className = 'tags'
      for (const tag of place.tags) {
        const chip = document.createElement('span')
        chip.textContent = tag
        tags.append(chip)
      }
      root.append(tags)
    }

    const actions = document.createElement('div')
    actions.className = 'popup-actions'

    const edit = document.createElement('button')
    edit.type = 'button'
    edit.textContent = 'Edit'
    edit.addEventListener('click', () => this.edit(place))

    const remove = document.createElement('button')
    remove.type = 'button'
    remove.className = 'bad'
    remove.title = 'Delete this pin'
    remove.textContent = 'Delete'
    remove.addEventListener('click', () => void this.remove(place))

    actions.append(edit, remove)
    root.append(actions)
    return root
  }

  private edit(place: Place): void {
    this.markers.get(place.id)?.togglePopup()
    this.pending?.marker.remove()

    const marker = new Marker({
      color: this.labelOf(place)?.colour ?? NO_LABEL_COLOUR,
      draggable: true,
    })
      .setLngLat([place.lon, place.lat])
      .addTo(this.map)

    marker.on('dragend', () => {
      const at = marker.getLngLat()
      if (this.pending) {
        this.pending.lat = at.lat
        this.pending.lon = at.lng
        this.showCoords()
      }
    })

    this.pending = { lat: place.lat, lon: place.lon, marker, editing: place.id }

    element<HTMLInputElement>('place-name').value = place.name
    element<HTMLInputElement>('place-tags').value = place.tags.join(', ')
    element<HTMLSelectElement>('place-label').value = String(place.label_id ?? '')
    element<HTMLSelectElement>('place-folder').value = String(place.folder_id ?? '')
    this.openForm('Edit pin')
  }

  private async remove(place: Place): Promise<void> {
    this.say(`Removing ${place.name} and putting the fog back…`)
    try {
      await apiSend('DELETE', `/api/places/${place.id}`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
    this.onChanged()
  }

  // ------------------------------------------------------------------- tree

  private async newFolder(): Promise<void> {
    const name = window.prompt('Folder name')?.trim()
    if (!name) return

    // A folder made while another is selected goes inside it, which is the
    // only way to make a subfolder without a second control.
    const selected = element<HTMLSelectElement>('place-folder').value
    const parent = selected ? this.folders.find((f) => f.id === Number(selected)) : undefined
    const parentId = parent && parent.parent_id === null ? parent.id : null

    try {
      await apiSend('POST', '/api/folders', { name, parent_id: parentId })
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
  }

  private async setFolder(folder: Folder, changes: Record<string, unknown>): Promise<void> {
    try {
      await apiSend('PATCH', `/api/folders/${folder.id}`, changes)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
  }

  private async removeFolder(folder: Folder): Promise<void> {
    const ok = window.confirm(
      `Delete the folder "${folder.name}"? Any pins inside become unfiled — ` +
        'nothing on the map is removed.',
    )
    if (!ok) return

    try {
      await apiSend('DELETE', `/api/folders/${folder.id}`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
  }

  private paintTree(): void {
    const tree = element('place-tree')
    tree.replaceChildren()
    element('place-empty').hidden = this.places.length > 0 || this.folders.length > 0

    const top = this.folders.filter((folder) => folder.parent_id === null)
    for (const folder of top) {
      tree.append(this.folderRow(folder, 0))
      if (this.collapsed.has(folder.id)) continue

      for (const place of this.placesIn(folder.id)) tree.append(this.placeRow(place, 1))
      for (const child of this.folders.filter((f) => f.parent_id === folder.id)) {
        tree.append(this.folderRow(child, 1))
        if (this.collapsed.has(child.id)) continue
        for (const place of this.placesIn(child.id)) tree.append(this.placeRow(place, 2))
      }
    }

    const unfiled = this.placesIn(null)
    if (unfiled.length) {
      const heading = document.createElement('div')
      heading.className = 'tree-row tree-folder'
      heading.dataset.depth = '0'
      const name = document.createElement('span')
      name.className = 'tree-name'
      name.textContent = 'Unfiled'
      const count = document.createElement('span')
      count.className = 'tree-count'
      count.textContent = String(unfiled.length)
      heading.append(name, count)
      tree.append(heading)
      for (const place of unfiled) tree.append(this.placeRow(place, 1))
    }
  }

  private placesIn(folderId: number | null): Place[] {
    return this.places
      .filter((place) => (place.folder_id ?? null) === folderId)
      .sort((a, b) => a.name.localeCompare(b.name))
  }

  private folderRow(folder: Folder, depth: number): HTMLElement {
    const row = document.createElement('div')
    row.className = 'tree-row tree-folder'
    row.dataset.depth = String(depth)
    row.dataset.hidden = String(!folder.visible)

    const collapsed = this.collapsed.has(folder.id)
    const twist = document.createElement('button')
    twist.type = 'button'
    twist.dataset.action = 'twist'
    twist.textContent = collapsed ? '▸' : '▾'
    twist.title = collapsed ? 'Expand' : 'Collapse'
    twist.addEventListener('click', () => {
      if (collapsed) this.collapsed.delete(folder.id)
      else this.collapsed.add(folder.id)
      this.paintTree()
    })

    const name = document.createElement('span')
    name.className = 'tree-name'
    name.textContent = folder.name
    name.title = 'Double click to rename'
    name.addEventListener('dblclick', () => {
      const renamed = window.prompt('Folder name', folder.name)?.trim()
      if (renamed && renamed !== folder.name) void this.setFolder(folder, { name: renamed })
    })

    const count = document.createElement('span')
    count.className = 'tree-count'
    count.textContent = String(this.countIn(folder))

    const eye = document.createElement('button')
    eye.type = 'button'
    eye.dataset.action = 'visible'
    eye.textContent = folder.visible ? '👁' : '🚫'
    eye.title = folder.visible ? 'Hide these pins' : 'Show these pins'
    eye.addEventListener('click', () => void this.setFolder(folder, { visible: !folder.visible }))

    const remove = document.createElement('button')
    remove.type = 'button'
    remove.dataset.action = 'delete'
    remove.textContent = '×'
    remove.title = 'Delete this folder'
    remove.addEventListener('click', () => void this.removeFolder(folder))

    row.append(twist, name, count, eye, remove)
    return row
  }

  /** Pins in a folder and everything under it, which is what a count means. */
  private countIn(folder: Folder): number {
    const children = this.folders.filter((item) => item.parent_id === folder.id)
    return (
      this.placesIn(folder.id).length +
      children.reduce((total, child) => total + this.placesIn(child.id).length, 0)
    )
  }

  private placeRow(place: Place, depth: number): HTMLElement {
    const row = document.createElement('div')
    row.className = 'tree-row'
    row.dataset.depth = String(depth)
    row.dataset.hidden = String(this.isHidden(place))

    const dot = document.createElement('span')
    dot.className = 'tree-dot'
    dot.style.background = this.labelOf(place)?.colour ?? NO_LABEL_COLOUR

    const name = document.createElement('span')
    name.className = 'tree-name'
    name.textContent = place.name
    name.title = 'Show on the map'
    name.addEventListener('click', () => {
      this.map.easeTo({ center: [place.lon, place.lat], zoom: Math.max(this.map.getZoom(), 15) })
      this.markers.get(place.id)?.togglePopup()
    })

    row.append(dot, name)
    return row
  }

  // ---------------------------------------------------------------- pickers

  private paintPickers(): void {
    const labels = element<HTMLSelectElement>('place-label')
    const chosenLabel = labels.value
    labels.replaceChildren(new Option('None', ''))
    for (const label of this.labels) labels.append(new Option(label.name, String(label.id)))
    labels.value = chosenLabel

    const folders = element<HTMLSelectElement>('place-folder')
    const chosenFolder = folders.value
    folders.replaceChildren(new Option('Unfiled', ''))
    for (const folder of this.folders.filter((item) => item.parent_id === null)) {
      folders.append(new Option(folder.name, String(folder.id)))
      for (const child of this.folders.filter((item) => item.parent_id === folder.id)) {
        folders.append(new Option(`  ${child.name}`, String(child.id)))
      }
    }
    folders.value = chosenFolder
  }

  private say(message: string, bad = false): void {
    const line = element('place-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}
