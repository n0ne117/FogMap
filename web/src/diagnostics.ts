// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the map thinks is going on. Written because "I see nothing" is a
// symptom shared by a dozen different causes, and everything checkable from
// the server can be correct while the browser quietly fails.

import type { Map as MapLibreMap } from 'maplibre-gl'

import { glyphsUrl, spriteUrl } from './map'
import type { MapTheme } from './theme'
import { element } from './ui'

const BASEMAP_SOURCE = 'protomaps'
const FOG_LAYER = 'fogmap-fog'

const errors: string[] = []
const seen: string[] = []
const created = performance.now()
let frames = 0

/**
 * Count animation frames.
 *
 * MapLibre defers loading a style to the next animation frame, so if these
 * never arrive the map stays blank for ever, reports no error, and creates no
 * layers at all - which is indistinguishable from a broken basemap. Browsers
 * suspend them in hidden tabs, and some remote or headless surfaces never
 * deliver them.
 */
export function countFrames(): void {
  const tick = () => {
    frames += 1
    window.requestAnimationFrame(tick)
  }
  window.requestAnimationFrame(tick)
}
const probes: Record<string, string> = {}

/** Record which map lifecycle events actually fired. */
export function watchLifecycle(map: MapLibreMap): void {
  const events = ['styledata', 'style.load', 'load', 'sourcedata', 'idle'] as const
  for (const name of events) {
    map.on(name as never, (() => {
      if (!seen.includes(name)) seen.push(name)
    }) as never)
  }
}

/**
 * Probe the things the style waits for.
 *
 * MapLibre blocks the style load on the sprite. If that request hangs rather
 * than fails - an offline machine, a proxy that black-holes it - no layers are
 * ever added and nothing reports an error, which looks exactly like an empty
 * map.
 */
export async function probe(styleSprite: string, glyphs: string): Promise<void> {
  const targets: Record<string, string> = {
    'sprite json': `${styleSprite}.json`,
    'sprite png': `${styleSprite}.png`,
    glyphs: glyphs
      .replace('{fontstack}', 'Noto%20Sans%20Regular')
      .replace('{range}', '0-255'),
    'basemap head': '/api/basemap/planet.pmtiles',
  }

  await Promise.all(
    Object.entries(targets).map(async ([label, url]) => {
      const started = performance.now()
      const controller = new AbortController()
      const timer = window.setTimeout(() => controller.abort(), 8000)
      try {
        const response = await fetch(url, {
          method: label === 'basemap head' ? 'HEAD' : 'GET',
          signal: controller.signal,
        })
        probes[label] = `${response.status} in ${Math.round(performance.now() - started)}ms`
      } catch (error) {
        const why = controller.signal.aborted ? 'TIMED OUT after 8s' : String(error)
        probes[label] = `FAILED ${why}`
      } finally {
        window.clearTimeout(timer)
      }
    }),
  )
}

export function recordMapError(message: string): void {
  if (message && !errors.includes(message)) errors.push(message)
  if (errors.length > 8) errors.shift()
}

function line(label: string, value: unknown): string {
  return `${label.padEnd(22)} ${String(value)}`
}

export function report(map: MapLibreMap, hasBasemap: boolean): string {
  const rows: string[] = []

  type Sketch = { layers?: unknown[]; sources?: Record<string, unknown> }
  let style: Sketch | undefined
  try {
    style = map.getStyle() as unknown as Sketch
  } catch {
    style = undefined
  }

  rows.push(line('basemap on disk', hasBasemap ? 'yes' : 'NO'))
  rows.push(line('style loaded', safe(() => map.isStyleLoaded())))
  rows.push(line('style layers', style?.layers?.length ?? 'style not readable'))
  rows.push(line('basemap source', style?.sources && BASEMAP_SOURCE in style.sources ? 'in style' : 'MISSING'))
  // Only ask once it exists: querying a missing source makes MapLibre emit
  // an error, which would then appear in this very report.
  rows.push(
    line(
      'basemap source loaded',
      map.getSource(BASEMAP_SOURCE)
        ? safe(() => map.isSourceLoaded(BASEMAP_SOURCE))
        : 'source not created yet',
    ),
  )
  rows.push(line('basemap layers drawn', countBasemapLayers(map)))
  rows.push(line('fog layer', map.getLayer(FOG_LAYER) ? 'present' : 'MISSING'))
  rows.push(
    line(
      'fog opacity',
      map.getLayer(FOG_LAYER)
        ? safe(() => map.getPaintProperty(FOG_LAYER, 'raster-opacity'))
        : 'layer not created yet',
    ),
  )
  rows.push(line('zoom', map.getZoom().toFixed(2)))
  rows.push(line('centre', `${map.getCenter().lng.toFixed(4)}, ${map.getCenter().lat.toFixed(4)}`))
  rows.push(line('canvas', `${map.getCanvas().width}x${map.getCanvas().height}`))

  rows.push(line('events fired', seen.length ? seen.join(', ') : 'NONE'))
  rows.push(
    line(
      'animation frames',
      frames === 0
        ? 'NONE - the map cannot start without these'
        : `${frames} since load`,
    ),
  )
  rows.push(line('page visible', document.visibilityState))
  rows.push(line('age', `${Math.round((performance.now() - created) / 1000)}s`))
  rows.push(line('webgl', webgl()))

  rows.push('')
  rows.push('reachability:')
  for (const [label, result] of Object.entries(probes)) {
    rows.push(`  ${label.padEnd(20)} ${result}`)
  }
  if (!Object.keys(probes).length) rows.push('  (probing, press Refresh again)')

  rows.push('')
  rows.push(errors.length ? `errors:\n  ${errors.join('\n  ')}` : 'errors                 none')
  return rows.join('\n')
}

function countBasemapLayers(map: MapLibreMap): string {
  try {
    const style = map.getStyle() as { layers?: { source?: string }[] }
    const all = style.layers ?? []
    const fromBasemap = all.filter((layer) => layer.source === BASEMAP_SOURCE)
    return `${fromBasemap.length} of ${all.length}`
  } catch {
    return 'unknown'
  }
}

function webgl(): string {
  try {
    const canvas = document.createElement('canvas')
    const gl =
      canvas.getContext('webgl2') ??
      (canvas.getContext('webgl') as WebGLRenderingContext | null)
    if (!gl) return 'NO CONTEXT'
    const info = gl.getExtension('WEBGL_debug_renderer_info')
    return info
      ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL))
      : 'available'
  } catch (error) {
    return `threw: ${error instanceof Error ? error.message : String(error)}`
  }
}

function safe(read: () => unknown): string {
  try {
    return String(read())
  } catch (error) {
    return `threw: ${error instanceof Error ? error.message : String(error)}`
  }
}

export function wireDiagnostics(
  map: MapLibreMap,
  hasBasemap: boolean,
  theme: MapTheme,
): void {
  const box = element('diagnostics')
  const refresh = () => {
    box.textContent = report(map, hasBasemap)
  }

  element('diagnostics-refresh').addEventListener('click', () => {
    void probe(spriteUrl(theme), glyphsUrl()).then(refresh)
    refresh()
  })
  element('diagnostics-copy').addEventListener('click', () => {
    void navigator.clipboard?.writeText(box.textContent ?? '')
  })

  map.on('load', refresh)
  map.on('idle', refresh)
  refresh()

  // Probe once on startup so the answer is already there when someone looks.
  void probe(spriteUrl(theme), glyphsUrl()).then(refresh)
}
