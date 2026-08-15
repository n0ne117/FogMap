// SPDX-License-Identifier: AGPL-3.0-or-later

import 'maplibre-gl/dist/maplibre-gl.css'

import type { Map as MapLibreMap } from 'maplibre-gl'

import {
  countFrames,
  recordMapError,
  watchLifecycle,
  wireDiagnostics,
} from './diagnostics'
import { Draw, MIN_DRAW_ZOOM, type Mode, type Tool } from './draw'
import { Imports } from './imports'
import {
  applyFogOpacity,
  applyMapTheme,
  applyView,
  basemapAvailable,
  bustTileCache,
  buildStyle,
  createMap,
  getFogOpacity,
  openArchive,
  pmtilesProtocol,
  setFogOpacity,
  type MapSetup,
} from './map'
import { Places } from './places'
import { Setup } from './setup'
import { Sources } from './sources'
import { Timeline } from './timeline'
import { Trails } from './trails'
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
import { element, radioGroup, Sheets, wireTabs, wireTokenField, wireZoom } from './ui'

const REPO_URL = 'https://github.com/n0ne117/FogMap'
const CHANGELOG_URL = `${REPO_URL}/blob/main/CHANGELOG.md`
const webVersion = __FOGMAP_VERSION__

function showVersion(): void {
  const corner = element<HTMLAnchorElement>('version-corner')
  corner.textContent = `v${webVersion}`
  corner.href = `${REPO_URL}/releases/tag/v${webVersion}`
  corner.title = `FogMap ${webVersion} — release notes`

  const link = element<HTMLAnchorElement>('version-link')
  link.textContent = `v${webVersion}`
  link.href = CHANGELOG_URL
}

/**
 * One version is enough. The api is mentioned only when it disagrees, which
 * is the only moment the distinction is worth anyone's attention.
 */
async function checkApiVersion(): Promise<void> {
  const mismatch = element('version-mismatch')
  try {
    const response = await fetch('/healthz', { headers: { accept: 'application/json' } })
    const body = (await response.json()) as { version?: string }
    const apiVersion = body.version ?? 'unknown'

    if (apiVersion === webVersion) return
    mismatch.textContent =
      `The api reports ${apiVersion} but this page was built from ` +
      `${webVersion}. One of the two containers is out of date.`
    mismatch.hidden = false
  } catch {
    mismatch.textContent = 'The api is unreachable.'
    mismatch.hidden = false
  }
}

function wireDrawing(
  map: MapLibreMap,
  options: MapSetup,
  timeline: Timeline,
  trails: Trails,
): Draw {
  const status = element('draw-status')
  const hint = element('draw-hint')
  const undoButton = element<HTMLButtonElement>('draw-undo')

  const draw = new Draw(
    map as never,
    () => {
      bustTileCache()
      applyView(map, options)
      void timeline.load()
      void trails.refresh()
      undoButton.disabled = draw.undoDepth === 0
    },
    (message, bad) => {
      status.textContent = message
      status.hidden = !message
      status.dataset.state = bad ? 'bad' : ''
      if (message && !bad) window.setTimeout(() => (status.hidden = true), 4000)
    },
  )
  draw.attach()

  let paintTool: (value: Tool) => void = () => {}

  /** Drawing below z14 produces meaningless geometry, so it is locked out. */
  const refreshLock = () => {
    const allowed = draw.canDraw
    const zoom = map.getZoom()

    for (const id of ['draw-tool', 'draw-mode']) {
      const group = element(id)
      group.dataset.locked = String(!allowed)
      for (const button of group.querySelectorAll('button')) button.disabled = !allowed
    }

    hint.textContent = allowed
      ? draw.activeTool === 'line'
        ? 'Click to add points, double click to finish.'
        : draw.activeTool === 'freehand'
          ? 'Drag on the map to draw.'
          : 'Pick a tool to start drawing.'
      : `Zoom to ${MIN_DRAW_ZOOM} or closer to draw. Currently ${zoom.toFixed(1)}.`

    if (!allowed && draw.activeTool !== 'off') {
      draw.setTool('off')
      paintTool('off')
    }
  }

  paintTool = radioGroup<Tool>('draw-tool', 'off', (value) => {
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

  // The pencil opens the toolbar. Closing it puts the brush away too, so the
  // map is never left in drawing mode with nothing on screen saying so.
  const bar = element('draw-bar')
  const toggle = element('draw-toggle')
  toggle.addEventListener('click', () => {
    const opening = bar.hidden
    bar.hidden = !opening
    toggle.setAttribute('aria-pressed', String(opening))
    if (!opening) {
      draw.setTool('off')
      paintTool('off')
    }
    refreshLock()
  })

  map.on('zoomend', refreshLock)
  map.on('load', refreshLock)
  refreshLock()
  return draw
}

async function start(): Promise<void> {
  countFrames()
  showVersion()
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
    const message = event.error?.message ?? String(event)
    console.warn('maplibre', message)
    recordMapError(message)

    // Tiles missing at the edge of the world are normal; anything about the
    // basemap or the style is worth putting in front of someone.
    if (/pmtiles|protomaps|style|source|glyph|sprite/i.test(message)) {
      const notice = element('map-error')
      notice.textContent = `Map problem: ${message}`
      notice.hidden = false
    }
  })

  const handle: Record<string, unknown> = { map, options, buildStyle, openArchive, pmtilesProtocol }
  ;(window as unknown as { fogmap: unknown }).fogmap = handle

  // The sheets are mutually exclusive: opening places closes settings.
  const sheets = new Sheets(['panel', 'places-page'])
  element('panel-toggle').addEventListener('click', () => sheets.toggle('panel'))
  element('panel-close').addEventListener('click', () => sheets.close())
  element('places-toggle').addEventListener('click', () => sheets.toggle('places-page'))
  element('places-close').addEventListener('click', () => sheets.close())
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') sheets.close()
  })

  wireTabs('tabs')
  wireZoom(map as never)
  watchLifecycle(map)
  wireDiagnostics(map, hasBasemap, options.theme)

  // Fog thickness is a viewing choice applied on the GPU: it changes as the
  // slider moves, with no re-render and no request to the server.
  const fogSlider = element<HTMLInputElement>('fog-opacity')
  const fogValue = element('fog-opacity-value')
  const paintFog = (percent: number) => {
    fogValue.textContent = `${Math.round(percent)}%`
  }
  fogSlider.value = String(Math.round(getFogOpacity() * 100))
  paintFog(Number(fogSlider.value))
  fogSlider.addEventListener('input', () => {
    const percent = Number(fogSlider.value)
    paintFog(percent)
    setFogOpacity(map, percent / 100)
  })

  radioGroup<UiTheme>('ui-theme', getUiTheme(), (value) => setUiTheme(value))
  radioGroup<MapTheme>('map-theme', getMapTheme(), (value) => {
    setMapTheme(value)
    options.theme = value
    applyMapTheme(map, options)
  })

  const trails = new Trails(map, (message) => {
    const notice = element('trail-notice')
    notice.textContent = message
    notice.hidden = !message
  })
  const attachTrails = () => {
    try {
      applyFogOpacity(map)
      trails.attach()
      void trails.refresh()
    } catch (error) {
      console.error('FogMap could not attach the trail layer', error)
    }
  }
  map.on('style.load', attachTrails)
  if (map.isStyleLoaded()) attachTrails()

  const timeline = new Timeline((view) => {
    options.view = view
    applyView(map, options)
    trails.view = view
    void trails.refresh()
  })
  void timeline.load()

  const draw = wireDrawing(map, options, timeline, trails)

  const places = new Places(map, () => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
  })
  places.wire()
  void places.load()

  const sources = new Sources()
  void sources.load()

  wireTokenField(() => void sources.load())

  const imports = new Imports(() => {
    bustTileCache()
    applyView(map, options)
    void timeline.load()
    void trails.refresh()
  })
  imports.wire()

  const setup = new Setup(() => {
    if (options.hasBasemap) return
    options.hasBasemap = true
    applyMapTheme(map, options)
    void timeline.load()
  })
  setup.wire()
  void setup.maybeShow()

  Object.assign(handle, {
    sheets,
    timeline,
    places,
    sources,
    trails,
    imports,
    draw,
    setup,
  })
}

void start()
