// SPDX-License-Identifier: AGPL-3.0-or-later

import 'maplibre-gl/dist/maplibre-gl.css'

import type { Map as MapLibreMap } from 'maplibre-gl'

import { Draw, MIN_DRAW_ZOOM, type Mode, type Tool } from './draw'
import {
  applyMapTheme,
  applyView,
  basemapAvailable,
  bustTileCache,
  buildStyle,
  createMap,
  type MapSetup,
} from './map'
import { Setup } from './setup'
import { Timeline } from './timeline'
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

function wireDrawing(
  map: MapLibreMap,
  options: MapSetup,
  timeline: Timeline,
): void {
  const status = element('draw-status')
  const hint = element('draw-hint')
  const undoButton = element<HTMLButtonElement>('draw-undo')

  const draw = new Draw(
    map as never,
    () => {
      // The tiles behind this view just changed on disk.
      bustTileCache()
      applyView(map, options)
      void timeline.load()
      undoButton.disabled = draw.undoDepth === 0
    },
    (message, bad) => {
      status.textContent = message
      status.hidden = !message
      status.dataset.state = bad ? 'bad' : ''
    },
  )
  draw.attach()

  /** Drawing below z14 produces meaningless geometry, so it is locked out. */
  const refreshLock = () => {
    const allowed = draw.canDraw
    const zoom = map.getZoom()

    for (const id of ['draw-tool', 'draw-mode']) {
      for (const button of element(id).querySelectorAll('button')) {
        button.disabled = !allowed
      }
    }
    element('draw-tool').dataset.locked = String(!allowed)
    element('draw-mode').dataset.locked = String(!allowed)

    hint.textContent = allowed
      ? draw.activeTool === 'line'
        ? 'Click to add points, double click to finish.'
        : draw.activeTool === 'freehand'
          ? 'Drag on the map to draw.'
          : ''
      : `Zoom in to ${MIN_DRAW_ZOOM} to draw. Currently ${zoom.toFixed(1)}.`

    if (!allowed && draw.activeTool !== 'off') {
      draw.setTool('off')
      paintGroup('draw-tool', 'off')
    }
  }

  const paintGroup = (id: string, value: string) => {
    for (const button of element(id).querySelectorAll('button')) {
      button.setAttribute('aria-pressed', String(button.dataset.value === value))
    }
  }

  radioGroup<Tool>('draw-tool', 'off', (value) => {
    draw.setTool(value)
    refreshLock()
  })
  radioGroup<Mode>('draw-mode', 'add', (value) => draw.setMode(value))

  element<HTMLInputElement>('draw-layers').addEventListener('input', (event) => {
    draw.layers = (event.target as HTMLInputElement).value
  })
  element<HTMLInputElement>('draw-radius').addEventListener('input', (event) => {
    const value = Number((event.target as HTMLInputElement).value)
    if (Number.isFinite(value) && value > 0) draw.radiusM = value
  })

  undoButton.addEventListener('click', () => void draw.undo())

  map.on('zoomend', refreshLock)
  map.on('load', refreshLock)
  refreshLock()

  const handle = (window as unknown as { fogmap: Record<string, unknown> }).fogmap
  if (handle) {
    handle.draw = draw
    handle.refreshDrawLock = refreshLock
  }
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
    view: Timeline.remembered(),
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

  const timeline = new Timeline((view) => {
    options.view = view
    applyView(map, options)
  })
  void timeline.load()

  wireDrawing(map, options, timeline)

  // Once the archive lands, rebuild the style so the basemap appears without
  // the user having to reload.
  const setup = new Setup(() => {
    if (options.hasBasemap) return
    options.hasBasemap = true
    applyMapTheme(map, options)
    void timeline.load()
  })
  setup.wire()
  void setup.maybeShow()

  ;(window as unknown as { fogmap: Record<string, unknown> }).fogmap.timeline =
    timeline
}

void start()
