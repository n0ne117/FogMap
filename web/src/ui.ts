// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Chrome that is not the map: the full-screen sheets, the settings tabs, the
// zoom control and the API token field.

import {
  ApiError,
  apiSend,
  clean,
  describeToken,
  getToken,
  setToken,
  tokenFrom,
} from './api'
import { getUiTheme } from './theme'

export function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) {
    throw new Error(
      `Irfaran is missing the element #${id}. index.html and the TypeScript are out of sync.`,
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
  acceptTokenPaste(input)
  input.addEventListener('input', () => {
    // Same forgiveness as the setup screen: a token has no whitespace in it,
    // so anything pasted alongside it is not part of it.
    setToken(tokenFrom(input.value))
    paint()
    onChange()
  })

  // Apply exists because storing on `input` alone is not enough.
  //
  // A password manager, an autofill, or any extension sets .value directly and
  // does not dispatch an input event - so the field visibly holds the right
  // token, nothing is stored, the status line says "No token set", and every
  // write is refused while the answer sits on screen in plain sight. A button
  // reads the field whatever put it there.
  //
  // It also verifies. Storing a token and finding out at the first edit is how
  // somebody ends up certain they entered it correctly and an app that
  // disagrees quietly.
  const apply = element<HTMLButtonElement>('token-apply')
  apply.addEventListener('click', () => {
    void (async () => {
      const candidate = tokenFrom(input.value)
      if (!candidate) {
        state.textContent = 'Paste the token first.'
        state.dataset.state = 'warn'
        return
      }

      const previous = getToken()
      apply.disabled = true
      state.textContent = 'Checking with the server.'
      state.dataset.state = ''
      setToken(candidate)

      // A browser with storage blocked accepts setToken silently and hands
      // back nothing, which would otherwise look like the token being wrong.
      if (getToken() !== candidate) {
        state.textContent =
          'This browser will not let the page remember anything, so the token ' +
          'cannot be kept. Private browsing usually does this.'
        state.dataset.state = 'bad'
        apply.disabled = false
        return
      }

      try {
        // Harmless: it writes back the theme this browser already has.
        await apiSend('PATCH', '/api/settings', { ui_theme: getUiTheme() })
        state.textContent = 'Token accepted by the server and kept in this browser.'
        state.dataset.state = 'good'
        onChange()
      } catch (error) {
        setToken(previous)
        // Name the server and describe what was sent. Which server is the
        // question that matters most - a token is per instance, and a browser
        // pointed somewhere other than you assumed looks exactly like a bad
        // token - and the length says at a glance whether something came along
        // with it.
        state.textContent =
          error instanceof ApiError && error.status === 401
            ? `${window.location.origin} refused that token ` +
              `(sent ${describeToken(candidate)}). Each Irfaran instance has ` +
              'its own token, so check this one came from that server: ' +
              'docker compose exec api python -m irfaran.cli token'
            : error instanceof Error
              ? error.message
              : String(error)
        state.dataset.state = 'bad'
        input.value = previous
      } finally {
        apply.disabled = false
      }
    })()
  })

  paint()
}

/** Take only the first line of a multi-line paste into a token field.
 *
 * `irfaran.cli token` prints the token on one line and where it came from on
 * the next, and the obvious thing to do with two lines of console output is to
 * select both. That cannot be repaired after the fact: a text input runs a
 * value sanitisation algorithm that strips CR and LF, so by the time anything
 * reads .value the two lines have been welded together with no whitespace
 * between them - "<token>from the environment ..." - and there is nothing left
 * to split on.
 *
 * The paste event still has the original text, newline intact, so that is
 * where the first line can be taken. Single-line pastes are left alone.
 */
export function acceptTokenPaste(input: HTMLInputElement): void {
  input.addEventListener('paste', (event: ClipboardEvent) => {
    const pasted = event.clipboardData?.getData('text') ?? ''
    if (!/[\r\n]/.test(pasted)) return

    event.preventDefault()
    const [firstLine = ''] = clean(pasted).trim().split(/\r?\n/)
    input.value = firstLine.trim()
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

/** Copy text to the clipboard, including over plain http.
 *
 * navigator.clipboard exists only in a secure context. A self-hosted instance
 * reached at http://tower:8080 is not one, and that is the normal case for
 * this project - so the modern API is tried, and execCommand catches the rest.
 * Deprecated, universally implemented, and the only thing that works there.
 *
 * Returns whether it worked, because a copy button that lies is worse than one
 * that admits it and lets you select the text yourself.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    // No permission, or an origin holding the object without the right to use
    // it. Fall through and try the old way.
  }

  const field = document.createElement('textarea')
  field.value = text
  field.setAttribute('readonly', '')
  field.style.position = 'fixed'
  field.style.top = '-1000px'
  document.body.append(field)
  field.select()
  try {
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    field.remove()
  }
}
