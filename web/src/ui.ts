// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Chrome that is not the map: the full-screen sheets, the settings tabs, the
// zoom control and the API token field.

import { getToken, setToken } from './api'

export function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) {
    throw new Error(
      `FogMap is missing the element #${id}. index.html and the TypeScript are out of sync.`,
    )
  }
  return found as T
}

/** Show one sheet at a time; opening one closes the others. */
export class Sheets {
  private readonly ids: string[]

  constructor(ids: string[]) {
    this.ids = ids
  }

  open(id: string): void {
    for (const other of this.ids) {
      element(other).hidden = other !== id
    }
  }

  close(): void {
    for (const id of this.ids) element(id).hidden = true
  }

  toggle(id: string): void {
    element(id).hidden ? this.open(id) : this.close()
  }

  get anyOpen(): boolean {
    return this.ids.some((id) => !element(id).hidden)
  }
}

export function wireTabs(navId: string): void {
  const nav = element(navId)
  const buttons = Array.from(nav.querySelectorAll<HTMLButtonElement>('button'))

  const show = (name: string) => {
    for (const button of buttons) {
      button.setAttribute('aria-selected', String(button.dataset.tab === name))
    }
    for (const panel of document.querySelectorAll<HTMLElement>('.tab-panel')) {
      panel.hidden = panel.dataset.tab !== name
    }
  }

  for (const button of buttons) {
    button.addEventListener('click', () => show(button.dataset.tab ?? ''))
  }
  show(buttons[0]?.dataset.tab ?? '')
}

/** Radio-style buttons whose value lives in a data attribute. */
export function radioGroup<T extends string>(
  id: string,
  current: T,
  onPick: (value: T) => void,
): (value: T) => void {
  const group = element(id)
  const buttons = Array.from(group.querySelectorAll<HTMLButtonElement>('button'))

  const paint = (value: T) => {
    for (const button of buttons) {
      button.setAttribute('aria-pressed', String(button.dataset.value === value))
    }
  }

  for (const button of buttons) {
    button.addEventListener('click', () => {
      const value = button.dataset.value as T
      paint(value)
      onPick(value)
    })
  }
  paint(current)
  return paint
}

interface ZoomTarget {
  getZoom(): number
  setZoom(zoom: number): unknown
  zoomIn(): unknown
  zoomOut(): unknown
  on(event: string, handler: () => void): unknown
}

/** The vertical zoom control at the left edge of the map. */
export function wireZoom(map: ZoomTarget): void {
  const slider = element<HTMLInputElement>('zoom-slider')
  const label = element('zoom-label')
  let fromSlider = false

  const paint = () => {
    const zoom = map.getZoom()
    if (!fromSlider) slider.value = zoom.toFixed(2)
    label.textContent = `z${zoom.toFixed(zoom < 10 ? 1 : 0)}`
  }

  slider.addEventListener('input', () => {
    fromSlider = true
    map.setZoom(Number(slider.value))
    fromSlider = false
    paint()
  })
  element('zoom-in').addEventListener('click', () => map.zoomIn())
  element('zoom-out').addEventListener('click', () => map.zoomOut())

  map.on('zoom', paint)
  map.on('zoomend', paint)
  paint()
}

/**
 * The API token field in settings.
 *
 * Everything that writes needs this, and before it existed the only place to
 * type one was the basemap setup screen - where it was labelled as being for
 * custom URLs, so nobody would look there when a data source toggle failed.
 */
export function wireTokenField(onChange: () => void): void {
  const input = element<HTMLInputElement>('settings-token')
  const state = element('token-state')

  const paint = () => {
    const token = getToken()
    state.textContent = token
      ? 'Token set in this browser.'
      : 'No token set. Anything that changes data will be refused.'
    state.dataset.state = token ? 'good' : 'warn'
  }

  input.value = getToken()
  input.addEventListener('input', () => {
    setToken(input.value.trim())
    paint()
    onChange()
  })
  paint()
}
