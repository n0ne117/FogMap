// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Importing workout files. Uploads run one at a time rather than all at once:
// each file is rasterised and re-rendered on the server, and firing a hundred
// of those in parallel would only make them all slower and the progress
// meaningless.

import { ApiError, getToken } from './api'
import { element } from './ui'

export interface ImportOutcome {
  name: string
  ok: boolean
  detail: string
}

interface IngestResult {
  events_created: number
  events_skipped: number
  points: number
  points_dropped: number
  tiles_touched: number
}

export class Imports {
  private readonly onDone: () => void
  private running = false

  constructor(onDone: () => void) {
    this.onDone = onDone
  }

  wire(): void {
    const picker = element<HTMLInputElement>('import-file')
    element('import-button').addEventListener('click', () => picker.click())
    picker.addEventListener('change', () => {
      const files = Array.from(picker.files ?? [])
      picker.value = ''
      if (files.length) void this.run(files)
    })
  }

  private async upload(file: File): Promise<ImportOutcome> {
    const path = file.name.toLowerCase().endsWith('.tcx')
      ? '/api/ingest/tcx'
      : '/api/ingest/gpx'

    const body = new FormData()
    body.append('file', file)

    const response = await fetch(path, {
      method: 'POST',
      headers: { 'X-FogMap-Token': getToken() },
      body,
    })

    const text = await response.text()
    let parsed: unknown = null
    try {
      parsed = JSON.parse(text)
    } catch {
      /* fall through to the raw text */
    }

    if (!response.ok) {
      const detail =
        parsed && typeof parsed === 'object' && 'detail' in parsed
          ? String((parsed as { detail: unknown }).detail)
          : `${response.status} ${response.statusText}`
      return { name: file.name, ok: false, detail }
    }

    const result = parsed as IngestResult
    const detail = result.events_created
      ? `${result.events_created} added, ${result.points} points`
      : 'already imported'
    return { name: file.name, ok: true, detail }
  }

  async run(files: File[]): Promise<void> {
    if (this.running) return
    if (!getToken()) {
      this.report([], 'No API token set. Add it under Settings, Data sources, API token.')
      return
    }

    this.running = true
    element('import-progress-row').hidden = false
    const bar = element<HTMLProgressElement>('import-progress')
    bar.max = files.length
    bar.value = 0

    const outcomes: ImportOutcome[] = []
    for (const [index, file] of files.entries()) {
      element('import-progress-text').textContent =
        `${index + 1} of ${files.length} — ${file.name}`

      try {
        outcomes.push(await this.upload(file))
      } catch (error) {
        outcomes.push({
          name: file.name,
          ok: false,
          detail: error instanceof ApiError ? error.message : String(error),
        })
      }

      bar.value = index + 1
      this.report(outcomes)
    }

    const failed = outcomes.filter((outcome) => !outcome.ok).length
    element('import-progress-text').textContent =
      `Done. ${outcomes.length - failed} of ${outcomes.length} imported` +
      (failed ? `, ${failed} failed` : '')

    this.running = false
    this.onDone()
  }

  private report(outcomes: ImportOutcome[], message = ''): void {
    const box = element('import-log')
    box.replaceChildren()

    if (message) {
      const row = document.createElement('div')
      row.className = 'import-row'
      row.dataset.state = 'bad'
      row.textContent = message
      box.append(row)
      return
    }

    // Newest first, and only the last handful - a thousand-file import should
    // not grow a thousand-row list.
    for (const outcome of outcomes.slice(-6).reverse()) {
      const row = document.createElement('div')
      row.className = 'import-row'
      row.dataset.state = outcome.ok ? 'good' : 'bad'
      row.textContent = `${outcome.name} — ${outcome.detail}`
      row.title = `${outcome.name} — ${outcome.detail}`
      box.append(row)
    }
  }
}
