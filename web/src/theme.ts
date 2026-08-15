// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Two independent settings, per section 7a. A dark interface with a light map
// is a valid combination and has to work, so these never read each other.

export type UiTheme = 'light' | 'dark' | 'system'
export type MapTheme = 'light' | 'dark'

const UI_KEY = 'irfaran.ui.theme'
const MAP_KEY = 'irfaran.map.theme'

const UI_VALUES: readonly UiTheme[] = ['light', 'dark', 'system']
const MAP_VALUES: readonly MapTheme[] = ['light', 'dark']

// The map defaults to dark independent of the interface, because fog of war
// reads better on a dark basemap. The light map is for reading street names.
const UI_DEFAULT: UiTheme = 'system'
const MAP_DEFAULT: MapTheme = 'dark'

const darkQuery = window.matchMedia('(prefers-color-scheme: dark)')

function read<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const stored = window.localStorage.getItem(key)
    if (stored && (allowed as readonly string[]).includes(stored)) {
      return stored as T
    }
  } catch {
    // Private browsing, or storage disabled. The default is still correct.
  }
  return fallback
}

function write(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Not being able to remember the choice is not a reason to refuse it.
  }
}

export function getUiTheme(): UiTheme {
  return read(UI_KEY, UI_VALUES, UI_DEFAULT)
}

export function getMapTheme(): MapTheme {
  return read(MAP_KEY, MAP_VALUES, MAP_DEFAULT)
}

/** Apply the interface theme to <html>. CSS does the rest. */
export function applyUiTheme(theme: UiTheme = getUiTheme()): void {
  document.documentElement.dataset.theme = theme
}

export function setUiTheme(theme: UiTheme): void {
  write(UI_KEY, theme)
  applyUiTheme(theme)
}

export function setMapTheme(theme: MapTheme): void {
  write(MAP_KEY, theme)
}

/**
 * Follow the operating system while the interface theme is `system`.
 * The listener stays attached: the user can change their system setting at
 * any time and the page should follow without a reload.
 */
export function watchSystemTheme(onChange: () => void): void {
  darkQuery.addEventListener('change', () => {
    if (getUiTheme() === 'system') {
      onChange()
    }
  })
}
