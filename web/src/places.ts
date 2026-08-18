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
import { icon } from './icons'
import { element } from './ui'

/** Ground a dropped pin clears, in metres. */
export const PLACE_RADIUS_M = 30

const NO_LABEL_COLOUR = '#8a8f98'

/** How deep folders nest. Mirrors organise.MAX_DEPTH on the server. */
const MAX_FOLDER_DEPTH = 2

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
  people: string[]
  lat: number
  lon: number
}

export interface Person {
  id: number
  name: string
}

interface PlacesResponse {
  places: Place[]
  labels: Label[]
  folders: Folder[]
}

interface PeopleResponse {
  people: Person[]
  named_on_pins: string[]
}

/** A pin being placed but not yet saved. */
interface Pending {
  lat: number
  lon: number
  marker: Marker
  popup?: Popup
  editing: number | null
}

export class Places {
  private readonly map: MapLibreMap
  private readonly onChanged: () => void

  private places: Place[] = []
  private labels: Label[] = []
  private folders: Folder[] = []
  private roster: string[] = []

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
      // The pins are the point; the roster only fills in the Who? choices. A
      // Promise.all here meant a hiccup fetching names emptied the whole tree,
      // which is a poor trade for a list of checkboxes.
      const body = await apiGet<PlacesResponse>('/api/places')
      const roster = await apiGet<PeopleResponse>('/api/people').catch(
        () => ({ people: [], named_on_pins: [] }) as PeopleResponse,
      )
      this.places = body.places ?? []
      this.labels = body.labels ?? []
      this.folders = body.folders ?? []
      // The registry, plus anyone named on a pin who is not on it - a name
      // taken off the list stays on the pins that recorded it.
      this.roster = Array.from(
        new Set([
          ...(roster.people ?? []).map((person) => person.name),
          ...(roster.named_on_pins ?? []),
        ]),
      ).sort((a, b) => a.localeCompare(b))
      this.say('')
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    this.paintTree()
    this.paintMarkers()
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
      if (!this.pending) return
      this.pending.lat = at.lat
      this.pending.lon = at.lng
      this.showCoords()
    })

    this.pending = { lat, lon, marker, editing: null }

    // The form opens at the pin, not in the sidebar. Somewhere on a map is a
    // position first and a row in a list second, and a form three hundred
    // pixels away from the thing it describes makes you hold the position in
    // your head while you type.
    const popup = new Popup({ offset: 26, closeOnClick: false, maxWidth: '19rem' })
      .setDOMContent(this.formFor(null))
    marker.setPopup(popup)
    marker.togglePopup()
    this.pending.popup = popup
    this.showCoords()
  }

  private showCoords(): void {
    if (!this.pending) return
    const line = this.pending.popup
      ?.getElement()
      ?.querySelector<HTMLElement>('.place-coords')
    if (line) {
      line.textContent =
        `${this.pending.lat.toFixed(6)}, ${this.pending.lon.toFixed(6)} — ` +
        `clears ${PLACE_RADIUS_M} m of fog`
    }
  }

  private cancel(): void {
    const editing = this.pending?.editing ?? null
    this.pending?.marker.remove()
    this.pending = null
    this.say('')

    // An edit borrowed the real pin's position, so put the pin back.
    if (editing !== null) void this.load()
  }

  /**
   * The form for a pin, as DOM, to be shown in a popup at the pin.
   *
   * Built here rather than in the markup because there is one of these per pin
   * and ids have to be unique - and because a title somebody typed goes in as
   * text, so a place called `<script>` is a place called `<script>`.
   */
  private formFor(place: Place | null): HTMLElement {
    const root = document.createElement('div')
    root.className = 'place-popup place-form-popup'

    const heading = document.createElement('h3')
    heading.textContent = place ? 'Edit pin' : 'New pin'
    root.append(heading)

    const title = field(root, 'Title', 'input')
    title.value = place?.name ?? ''
    title.placeholder = "Grandparents' flat"

    const label = field(root, 'Label', 'select')
    label.append(new Option('None', ''))
    for (const item of this.labels) label.append(new Option(item.name, String(item.id)))
    label.value = String(place?.label_id ?? '')

    const who = this.whoPicker(root, place?.people ?? [])

    const tags = field(root, 'Tags', 'input')
    tags.value = (place?.tags ?? []).join(', ')
    tags.placeholder = 'childhood, summer'

    const folder = field(root, 'Folder', 'select')
    folder.append(new Option('Unfiled', ''))
    for (const top of this.folders.filter((item) => item.parent_id === null)) {
      folder.append(new Option(top.name, String(top.id)))
      for (const child of this.folders.filter((item) => item.parent_id === top.id)) {
        folder.append(new Option(`\u00a0\u00a0${child.name}`, String(child.id)))
      }
    }
    folder.value = String(place?.folder_id ?? '')

    const coords = document.createElement('p')
    coords.className = 'place-coords hint'
    if (place) {
      coords.textContent = `${place.lat.toFixed(6)}, ${place.lon.toFixed(6)}`
    }
    root.append(coords)

    const actions = document.createElement('div')
    actions.className = 'popup-actions'

    const save = document.createElement('button')
    save.type = 'button'
    save.className = 'primary'
    save.textContent = 'Save'
    save.addEventListener('click', () => {
      void this.save({
        name: title.value.trim(),
        label_id: label.value || null,
        folder_id: folder.value || null,
        tags: tags.value,
        people: who(),
      })
    })

    const cancel = document.createElement('button')
    cancel.type = 'button'
    cancel.textContent = 'Cancel'
    cancel.addEventListener('click', () => this.cancel())

    const message = document.createElement('p')
    message.className = 'place-popup-message status-line'
    message.hidden = true

    actions.append(save, cancel)
    root.append(actions, message)

    window.setTimeout(() => title.focus(), 0)
    return root
  }

  /**
   * Who was there: a checkbox each, because it is multiple choice.
   *
   * A registry rather than free text, so the same person is spelled the same
   * way on every pin - and anyone already named on a pin appears here even if
   * they have since been taken off the list, or they would silently vanish the
   * next time the pin was saved.
   */
  private whoPicker(root: HTMLElement, chosen: string[]): () => string[] {
    const wrap = document.createElement('div')
    wrap.className = 'field'

    const caption = document.createElement('span')
    caption.textContent = 'Who?'
    wrap.append(caption)

    const list = document.createElement('div')
    list.className = 'who-list'

    const names = Array.from(new Set([...this.roster, ...chosen])).sort((a, b) =>
      a.localeCompare(b),
    )

    if (!names.length) {
      const empty = document.createElement('p')
      empty.className = 'hint'
      empty.textContent = 'Nobody registered yet — add names under Settings, Places.'
      list.append(empty)
    }

    const boxes: HTMLInputElement[] = []
    for (const name of names) {
      const row = document.createElement('label')
      row.className = 'check who-row'

      const box = document.createElement('input')
      box.type = 'checkbox'
      box.value = name
      box.checked = chosen.includes(name)
      boxes.push(box)

      const text = document.createElement('span')
      text.textContent = name

      row.append(box, text)
      list.append(row)
    }

    wrap.append(list)
    root.append(wrap)
    return () => boxes.filter((box) => box.checked).map((box) => box.value)
  }

  private async save(values: {
    name: string
    label_id: string | null
    folder_id: string | null
    tags: string
    people: string[]
  }): Promise<void> {
    if (!this.pending) return

    if (!values.name) {
      this.sayInPopup('A pin needs a title.', true)
      return
    }

    const body = {
      ...values,
      lat: this.pending.lat,
      lon: this.pending.lon,
      radius_m: PLACE_RADIUS_M,
    }

    const editing = this.pending.editing
    this.sayInPopup(editing ? 'Saving…' : 'Saving, and clearing the fog…')

    try {
      if (editing) {
        await apiSend('PATCH', `/api/places/${editing}`, body)
      } else {
        await apiSend('POST', '/api/places', body)
      }
    } catch (error) {
      this.sayInPopup(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    this.pending.marker.remove()
    this.pending = null
    this.say('')

    await this.load()
    this.onChanged()
  }

  /** Say it in the popup being filled in, not three hundred pixels away. */
  private sayInPopup(message: string, bad = false): void {
    const line = this.pending?.popup
      ?.getElement()
      ?.querySelector<HTMLElement>('.place-popup-message')
    if (!line) {
      this.say(message, bad)
      return
    }
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }

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

    if (place.people.length) {
      const who = document.createElement('div')
      who.className = 'place-people'
      who.textContent = `With ${place.people.join(', ')}`
      root.append(who)
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
    edit.append(icon('pencil', 14), text('Edit'))
    edit.addEventListener('click', () => this.edit(place))

    const remove = document.createElement('button')
    remove.type = 'button'
    remove.className = 'bad'
    remove.title = 'Delete this pin'
    remove.append(icon('trash', 14), text('Delete'))
    remove.addEventListener('click', () => void this.remove(place))

    actions.append(edit, remove)
    root.append(actions)
    return root
  }

  /**
   * Edit a pin where it is.
   *
   * The saved marker is swapped for a draggable one carrying the form, so the
   * position can be corrected in the same gesture as the title - and so the
   * thing being edited is the thing under the cursor rather than a row in a
   * list on the other side of the screen.
   */
  private edit(place: Place): void {
    this.markers.get(place.id)?.remove()
    this.markers.delete(place.id)
    this.pending?.marker.remove()

    const marker = new Marker({
      color: this.labelOf(place)?.colour ?? NO_LABEL_COLOUR,
      draggable: true,
    })
      .setLngLat([place.lon, place.lat])
      .addTo(this.map)

    marker.on('dragend', () => {
      const at = marker.getLngLat()
      if (!this.pending) return
      this.pending.lat = at.lat
      this.pending.lon = at.lng
      this.showCoords()
    })

    const popup = new Popup({ offset: 26, closeOnClick: false, maxWidth: '19rem' })
      .setDOMContent(this.formFor(place))
    marker.setPopup(popup)
    marker.togglePopup()

    this.pending = { lat: place.lat, lon: place.lon, marker, editing: place.id, popup }
    this.showCoords()
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

  /**
   * Make a folder, optionally inside another.
   *
   * The parent is passed in from the row that was clicked. It used to be
   * inferred from the pin form's folder picker, which is only on screen while a
   * pin is being dropped - so making a subfolder meant dropping a pin you did
   * not want, selecting a parent, and pressing a button somewhere else. Nobody
   * was going to find that.
   */
  private async newFolder(parentId: number | null = null): Promise<void> {
    const parent = parentId
      ? this.folders.find((item) => item.id === parentId)
      : undefined
    const asked = parent ? `New folder inside ${parent.name}` : 'New folder'

    const name = window.prompt(asked)?.trim()
    if (!name) return

    try {
      await apiSend('POST', '/api/folders', { name, parent_id: parentId })
      this.say(parent ? `${name} added inside ${parent.name}.` : `${name} added.`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    // A new subfolder is invisible if its parent happens to be collapsed.
    if (parentId) this.collapsed.delete(parentId)
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
    eye.append(icon(folder.visible ? 'eye' : 'eye-off'))
    eye.title = folder.visible ? 'Hide these pins' : 'Show these pins'
    eye.setAttribute('aria-label', eye.title)
    eye.addEventListener('click', () => void this.setFolder(folder, { visible: !folder.visible }))

    // A folder inside a folder was there all along and nothing said so: the
    // only way in was the New folder button and a parent picker that looked
    // like decoration. A plus on the row you want it under says it plainly.
    const nest = document.createElement('button')
    nest.type = 'button'
    nest.dataset.action = 'nest'
    nest.append(icon('plus'))
    const room = depth + 1 < MAX_FOLDER_DEPTH
    nest.title = room
      ? `New folder inside ${folder.name}`
      : `${folder.name} is already as deep as folders go`
    nest.setAttribute('aria-label', nest.title)
    nest.disabled = !room
    nest.addEventListener('click', () => void this.newFolder(folder.id))

    const remove = document.createElement('button')
    remove.type = 'button'
    remove.dataset.action = 'delete'
    remove.append(icon('trash'))
    remove.title = 'Delete this folder'
    remove.setAttribute('aria-label', remove.title)
    remove.addEventListener('click', () => void this.removeFolder(folder))

    row.append(twist, name, count, nest, eye, remove)
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

  private say(message: string, bad = false): void {
    const line = element('place-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}

/**
 * One labelled control inside a popup form.
 *
 * Returns the input so the caller can fill and read it, which keeps the form
 * building linear instead of a pile of createElement calls.
 */
function field(root: HTMLElement, caption: string, kind: 'input'): HTMLInputElement
function field(root: HTMLElement, caption: string, kind: 'select'): HTMLSelectElement
function field(
  root: HTMLElement,
  caption: string,
  kind: 'input' | 'select',
): HTMLInputElement | HTMLSelectElement {
  const wrap = document.createElement('label')
  wrap.className = 'field'

  const text = document.createElement('span')
  text.textContent = caption

  const control = document.createElement(kind) as HTMLInputElement | HTMLSelectElement
  if (kind === 'input') (control as HTMLInputElement).type = 'text'

  wrap.append(text, control)
  root.append(wrap)
  return control
}

/** A label beside an icon inside a button. */
function text(caption: string): HTMLSpanElement {
  const span = document.createElement('span')
  span.textContent = caption
  return span
}
