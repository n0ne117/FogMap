// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Taking the archive somewhere else, and bringing it back.
//
// The export is fetched rather than linked, because the endpoint needs the
// token and a plain link cannot send a header. So the file is pulled into a
// blob and handed to the browser from there - which also means a failure
// shows up as a message rather than as a downloaded page of JSON saying 401.

import { ApiError, apiGet, getToken } from './api'
import { estimate, runRender } from './render'
import { element } from './ui'

interface ImportResult {
  added: Record<string, number>
  skipped: Record<string, number>
  tiles_touched: number
  render_pending: number
  manifest: { counts?: Record<string, number>; exported_at?: string }
}

export class Backup {
  private readonly onChanged: () => void
  private busy = false

  constructor(onChanged: () => void) {
    this.onChanged = onChanged
  }

  wire(): void {
    element('export-start').addEventListener('click', () => void this.export())

    const picker = element<HTMLInputElement>('backup-file')
    element('backup-import-start').addEventListener('click', () => picker.click())
    picker.addEventListener('change', () => {
      const file = picker.files?.[0]
      picker.value = ''
      if (file) void this.import(file)
    })
  }

  private async export(): Promise<void> {
    const button = element<HTMLButtonElement>('export-start')
    if (!getToken()) {
      this.say('export-message', 'No API token set. Add it under Settings, Data sources.', true)
      return
    }

    button.disabled = true
    this.say('export-message', 'Packing everything up.')
    try {
      const response = await fetch('/api/export', {
        headers: { 'X-Irfaran-Token': getToken() },
      })
      if (!response.ok) {
        throw new ApiError(response.status, `${response.status} ${response.statusText}`)
      }

      const blob = await response.blob()
      const name =
        response.headers
          .get('content-disposition')
          ?.match(/filename="([^"]+)"/)?.[1] ?? 'irfaran-backup.irfaran'

      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = name
      link.click()
      URL.revokeObjectURL(url)

      this.say('export-message', `Saved ${name}, ${format(blob.size)}.`)
    } catch (error) {
      this.say(
        'export-message',
        error instanceof ApiError ? error.message : String(error),
        true,
      )
    } finally {
      button.disabled = false
    }
  }

  private async import(file: File): Promise<void> {
    if (this.busy) return
    this.busy = true

    const button = element<HTMLButtonElement>('backup-import-start')
    button.disabled = true
    this.say('backup-import-message', `Reading ${file.name}.`)

    try {
      const body = new FormData()
      body.append('file', file)

      const response = await fetch('/api/import', {
        method: 'POST',
        headers: { 'X-Irfaran-Token': getToken() },
        body,
      })
      const parsed = (await response.json()) as ImportResult & { detail?: string }
      if (!response.ok) {
        throw new ApiError(response.status, parsed.detail ?? `${response.status}`)
      }

      const { added, skipped } = parsed
      const summary =
        added.events || skipped.events
          ? `${added.events} tracks added` +
            (skipped.events ? `, ${skipped.events} already here` : '') +
            (added.places ? `, ${added.places} pins` : '') +
            (added.folders ? `, ${added.folders} folders` : '')
          : 'Nothing new in that file'

      if (!parsed.render_pending) {
        this.say('backup-import-message', `${summary}. Nothing to redraw.`)
        this.onChanged()
        return
      }

      await this.redraw(summary)
      this.onChanged()
    } catch (error) {
      this.say(
        'backup-import-message',
        error instanceof ApiError ? error.message : String(error),
        true,
      )
    } finally {
      button.disabled = false
      this.busy = false
    }
  }

  private async redraw(summary: string): Promise<void> {
    const row = element('backup-progress-row')
    const bar = element<HTMLProgressElement>('backup-progress')
    const text = element('backup-progress-text')

    row.hidden = false
    bar.value = 0
    bar.max = 1
    this.say('backup-import-message', `${summary}. Drawing the map…`)

    const startedAt = Date.now()
    await runRender((step) => {
      if (!step.total) return
      bar.max = step.total
      bar.value = step.done
      text.textContent = `Drawing the map — ${estimate(step, startedAt)}`
    })

    row.hidden = true
    this.say('backup-import-message', `${summary}. Done.`)
  }

  /** Is there an archive here already? The setup screen asks before offering. */
  static async isEmpty(): Promise<boolean> {
    try {
      const meta = await apiGet<{ counts: { events: number } }>('/api/meta')
      return (meta.counts?.events ?? 0) === 0
    } catch {
      return false
    }
  }

  private say(id: string, message: string, bad = false): void {
    const line = element(id)
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}

function format(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['kB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(1)} ${units[unit]}`
}
