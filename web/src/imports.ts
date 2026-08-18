// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Importing workout files. Uploads run one at a time rather than all at once,
// so progress means something and one bad file is attributable.
//
// Rendering is deferred to the end. It costs roughly the whole archive rather
// than the file just added, so paying it per file made a few hundred workouts
// take longer than an afternoon - the server notes which tiles went stale and
// settles the whole debt in one pass once the last file is in.

import { ApiError, getToken } from './api'
import { describeCost, describeRemaining, renderState, runRender } from './render'
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
    const kind = file.name.toLowerCase().endsWith('.tcx') ? 'tcx' : 'gpx'
    const path = `/api/ingest/${kind}?defer_render=true`

    const body = new FormData()
    body.append('file', file)

    const response = await fetch(path, {
      method: 'POST',
      headers: { 'X-Irfaran-Token': getToken() },
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
      this.report([], 'No API token set. Add it under Settings, Security.')
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
    const imported = `${outcomes.length - failed} of ${outcomes.length} imported` +
      (failed ? `, ${failed} failed` : '')

    // One render for the whole batch. It takes long enough on a real archive
    // that the bar is worth reusing for it - the files are all in by now, and
    // what is left to wait for is the drawing.
    try {
      await this.render(imported)
      element('import-progress-text').textContent = `Done. ${imported}`
    } catch (error) {
      element('import-progress-text').textContent =
        `${imported}, but the map could not be redrawn: ` +
        (error instanceof ApiError ? error.message : String(error))
    }

    this.running = false
    this.onDone()
  }

  /**
   * Run the deferred render, driving the progress bar from its own report.
   *
   * The response is newline-delimited JSON, a line per finished unit of work,
   * so the bar starts again from zero and means something the whole way
   * rather than sitting full while the server is still busy.
   */
  private async render(imported: string): Promise<void> {
    const bar = element<HTMLProgressElement>('import-progress')
    const text = element('import-progress-text')

    bar.value = 0
    bar.max = 1

    // Say what the wait is before it starts. The size is knowable up front and
    // a long-distance track makes this minutes rather than seconds.
    const size = describeCost(await renderState())
    text.textContent = size
      ? `${imported}. Drawing the map — ${size}.`
      : `${imported}. Drawing the map…`

    const finished = await runRender((state) => {
      if (state.total) {
        bar.max = state.total
        bar.value = state.done
      }
      text.textContent =
        `${imported}. Drawing the map — ${describeRemaining(state)}. ` +
        'This carries on if you close the browser.'
    })

    if (finished?.state === 'failed') {
      throw new ApiError(500, finished.error || 'The render stopped.')
    }
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
