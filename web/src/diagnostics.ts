// SPDX-License-Identifier: AGPL-3.0-or-later
//
// What the map thinks is going on. Written because "I see nothing" is a
// symptom shared by a dozen different causes, and everything checkable from
// the server can be correct while the browser quietly fails.

import type { Map as MapLibreMap } from 'maplibre-gl'

import { element } from './ui'

const BASEMAP_SOURCE = 'protomaps'
const FOG_LAYER = 'fogmap-fog'

const errors: string[] = []

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

function safe(read: () => unknown): string {
  try {
    return String(read())
  } catch (error) {
    return `threw: ${error instanceof Error ? error.message : String(error)}`
  }
}

export function wireDiagnostics(map: MapLibreMap, hasBasemap: boolean): void {
  const box = element('diagnostics')
  const refresh = () => {
    box.textContent = report(map, hasBasemap)
  }

  element('diagnostics-refresh').addEventListener('click', refresh)
  element('diagnostics-copy').addEventListener('click', () => {
    void navigator.clipboard?.writeText(box.textContent ?? '')
  })

  map.on('load', refresh)
  map.on('idle', refresh)
  refresh()
}
