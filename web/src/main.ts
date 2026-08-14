// SPDX-License-Identifier: AGPL-3.0-or-later

import 'maplibre-gl/dist/maplibre-gl.css'

import type { Map as MapLibreMap } from 'maplibre-gl'

import {
  applyMapTheme,
  basemapAvailable,
  buildStyle,
  createMap,
  type MapSetup,
} from './map'
import { Setup } from './setup'
import {
  applyUiTheme,
  getMapTheme,
  getUiTheme,
  setMapTheme,
  setUiTheme,
  watchSystemTheme,
  type MapTheme,
  type UiTheme,
} from './theme'

const REPO_URL = 'https://github.com/n0ne117/FogMap'
const webVersion = __FOGMAP_VERSION__

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) {
    throw new Error(
      `FogMap web is missing the element #${id}. index.html and main.ts are out of sync.`,
    )
  }
  return found as T
}

function showWebVersion(): void {
  const corner = element<HTMLAnchorElement>('version-corner')
  corner.textContent = `v${webVersion}`
  corner.href = `${REPO_URL}/releases/tag/v${webVersion}`
  corner.title = `FogMap ${webVersion} - release notes`
}

async function checkApiVersion(): Promise<void> {
  const target = element('api-version')
  try {
    const response = await fetch('/healthz', { headers: { accept: 'application/json' } })
    const body = (await response.json()) as { version?: string }
    const apiVersion = body.version ?? 'unknown'
    target.textContent =
      apiVersion === webVersion ? apiVersion : `${apiVersion} - does not match web`
    target.dataset.state = apiVersion === webVersion ? 'good' : 'warn'
  } catch {
    target.textContent = 'unreachable'
    target.dataset.state = 'bad'
  }
}

/** Wire a set of radio-style buttons whose values live in a data attribute. */
function radioGroup<T extends string>(
  id: string,
  current: T,
  onPick: (value: T) => void,
): void {
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
}

async function start(): Promise<void> {
  showWebVersion()
  void checkApiVersion()

  applyUiTheme()
  watchSystemTheme(() => applyUiTheme())

  const hasBasemap = await basemapAvailable()

  const options: MapSetup = {
    container: 'map',
    theme: getMapTheme(),
    view: 'all',
    hasBasemap,
  }

  let map: MapLibreMap
  try {
    map = createMap(options)
  } catch (error) {
    element('map-error').hidden = false
    console.error('FogMap could not create the map', error)
    return
  }

  map.on('error', (event) => {
    // Tile 404s are normal at the edges of the world; only surface real ones.
    console.warn('maplibre', event.error?.message ?? event)
  })

  // A handle for the browser console. Debugging a map without one means
  // rebuilding the whole bundle every time a question comes up, and the
  // frontend has no dev server. buildStyle is pure, so it can be called here
  // to inspect exactly what a theme would produce.
  ;(window as unknown as { fogmap: unknown }).fogmap = {
    map,
    setup: options,
    buildStyle,
  }

  radioGroup<UiTheme>('ui-theme', getUiTheme(), (value) => setUiTheme(value))
  radioGroup<MapTheme>('map-theme', getMapTheme(), (value) => {
    setMapTheme(value)
    options.theme = value
    applyMapTheme(map, options)
  })

  element('panel-toggle').addEventListener('click', () => {
    const panel = element('panel')
    panel.hidden = !panel.hidden
  })

  // Once the archive lands, rebuild the style so the basemap appears without
  // the user having to reload.
  const setup = new Setup(() => {
    if (options.hasBasemap) return
    options.hasBasemap = true
    applyMapTheme(map, options)
  })
  setup.wire()
  void setup.maybeShow()
}

void start()
