// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Workout trackers. Services that already hold your activities and hand them
// over when asked - the opposite direction from the live sources, which push.
//
// The API key is write-only from here. The server never returns it, so the
// field is blank on every load and an empty field means "keep what you have"
// rather than "clear it". Anything else would wipe the key every time somebody
// changed the sync interval.

import { ApiError, apiGet, apiSend, getToken } from './api'
import { estimate, readNdjson, runRender } from './render'
import { element } from './ui'

interface TrackerState {
  tracker: string
  enabled: boolean
  key_set: boolean
  athlete_id: string
  sync_hours: number
  since_days: number
  last_sync: string
  last_result: string
  last_error: string
  due: boolean
}

interface SyncSummary {
  imported: number
  already_here: number
  no_gps: number
  failed: number
  events: number
  tiles_touched: number
  summary: string
  notes: string[]
}

const NAME = 'intervals'

export class Trackers {
  private readonly onChanged: () => void
  private busy = false

  constructor(onChanged: () => void) {
    this.onChanged = onChanged
  }

  wire(): void {
    element('intervals-save').addEventListener('click', () => void this.save())
    element('intervals-sync').addEventListener('click', () => void this.sync())

    // Switching it on is the one thing worth saving without a second click.
    element<HTMLInputElement>('intervals-enabled').addEventListener('change', () => {
      void this.save()
    })
  }

  async load(): Promise<void> {
    try {
      const body = await apiGet<{ trackers: TrackerState[] }>('/api/trackers')
      const state = body.trackers.find((one) => one.tracker === NAME)
      if (state) this.render(state)
    } catch {
      /* the settings page is still usable without this */
    }
  }

  private render(state: TrackerState): void {
    element<HTMLInputElement>('intervals-enabled').checked = state.enabled
    element<HTMLInputElement>('intervals-athlete').value = state.athlete_id
    element<HTMLInputElement>('intervals-hours').value = String(state.sync_hours)
    element<HTMLInputElement>('intervals-days').value = String(state.since_days)
    // Never populated: the server will not say what the key is.
    element<HTMLInputElement>('intervals-key').value = ''

    const line = element('intervals-state')
    if (!state.key_set) {
      line.textContent = 'No API key set, so nothing will be fetched.'
      line.dataset.state = 'warn'
      return
    }

    const parts = [`Key set${state.enabled ? '' : ', tracker off'}`]
    if (state.last_sync) {
      parts.push(`last checked ${when(state.last_sync)}`)
    } else {
      parts.push('never checked')
    }
    if (state.last_result) parts.push(state.last_result)

    line.textContent = `${parts.join(' · ')}.`
    line.dataset.state = state.last_error ? 'bad' : state.enabled ? 'good' : ''
    if (state.last_error) line.textContent = state.last_error
  }

  private async save(): Promise<void> {
    const payload: Record<string, string> = {
      enabled: element<HTMLInputElement>('intervals-enabled').checked ? 'true' : 'false',
      athlete_id: element<HTMLInputElement>('intervals-athlete').value.trim() || '0',
      sync_hours: element<HTMLInputElement>('intervals-hours').value.trim() || '12',
      since_days: element<HTMLInputElement>('intervals-days').value.trim() || '30',
    }

    const key = element<HTMLInputElement>('intervals-key').value.trim()
    if (key) payload.api_key = key

    try {
      await apiSend('PATCH', `/api/trackers/${NAME}`, payload)
      this.say('Saved.')
      await this.load()
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      await this.load()
    }
  }

  private async sync(): Promise<void> {
    if (this.busy) return
    this.busy = true

    const button = element<HTMLButtonElement>('intervals-sync')
    button.disabled = true

    const row = element('intervals-progress-row')
    const bar = element<HTMLProgressElement>('intervals-progress')
    const text = element('intervals-progress-text')

    try {
      // Save first, so pressing Sync after typing a key does what it looks
      // like it does rather than syncing with the previous settings.
      await this.save()

      this.say('Asking intervals.icu what is new…')
      row.hidden = false
      bar.removeAttribute('value')
      text.textContent = 'Listing your activities.'

      const summary = await this.stream((step) => {
        if (step.stage === 'listed') {
          const total = Number(step.total ?? 0)
          bar.max = Math.max(1, total)
          bar.value = 0
          text.textContent = total
            ? `Checking ${total} ${total === 1 ? 'activity' : 'activities'}.`
            : 'Nothing in that window.'
          return
        }

        if (step.stage === 'activity') {
          const done = Number(step.done ?? 0)
          const total = Number(step.total ?? 0)
          bar.max = Math.max(1, total)
          bar.value = done
          const found = Number(step.imported ?? 0)
          text.textContent =
            `Activity ${done} of ${total}` + (found ? ` — ${found} new so far` : '')
        }
      })

      row.hidden = true
      if (!summary) return

      if (!summary.tiles_touched) {
        this.say(`${capital(summary.summary)}. Nothing to draw.`)
        await this.load()
        return
      }

      await this.draw(summary.summary)
      this.onChanged()
      await this.load()
    } catch (error) {
      row.hidden = true
      this.say(error instanceof ApiError ? error.message : String(error), true)
      await this.load()
    } finally {
      button.disabled = false
      this.busy = false
    }
  }

  /**
   * Follow the sync, reporting each activity as it lands.
   *
   * Downloading a month of activities is a minute or more, and a minute of
   * nothing is indistinguishable from a hang - to a person watching, and to a
   * reverse proxy deciding a request has stalled. Returns the final summary,
   * or null if the server reported an error part way through.
   */
  private async stream(
    onStep: (step: Record<string, unknown>) => void,
  ): Promise<SyncSummary | null> {
    const response = await fetch(`/api/trackers/${NAME}/sync`, {
      method: 'POST',
      headers: { 'X-Irfaran-Token': getToken() },
    })

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`
      try {
        detail = ((await response.json()) as { detail?: string }).detail ?? detail
      } catch {
        /* not JSON, keep the status line */
      }
      throw new ApiError(response.status, detail)
    }

    let summary: SyncSummary | null = null
    let failure = ''

    await readNdjson(response, (step) => {
      if (step.stage === 'error') {
        failure = String(step.error ?? 'The sync failed.')
        return
      }
      if (step.finished) {
        summary = step as unknown as SyncSummary
        return
      }
      onStep(step)
    })

    if (failure) throw new ApiError(502, failure)
    return summary
  }

  private async draw(summary: string): Promise<void> {
    const row = element('intervals-progress-row')
    const bar = element<HTMLProgressElement>('intervals-progress')
    const text = element('intervals-progress-text')

    row.hidden = false
    bar.value = 0
    bar.max = 1
    const startedAt = Date.now()

    await runRender((step) => {
      if (!step.total) return
      bar.max = step.total
      bar.value = step.done
      text.textContent = `Drawing what arrived — ${estimate(step, startedAt)}`
    })

    row.hidden = true
    this.say(`${capital(summary)}. Drawn.`)
  }

  private say(message: string, bad = false): void {
    const line = element('intervals-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}

function capital(text: string): string {
  return text ? text[0].toUpperCase() + text.slice(1) : text
}

/** A stored timestamp as something readable, falling back to the raw value. */
function when(stamp: string): string {
  const parsed = new Date(stamp)
  if (Number.isNaN(parsed.getTime())) return stamp
  return parsed.toLocaleString()
}
