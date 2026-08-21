// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Reading place names out of the basemap, and watching it happen.
//
// Two builds with the same shape and very different costs: towns take a couple
// of minutes, points of interest take hours because they only exist in the
// deepest tiles. So this is one poller painting two blocks, plus a line on the
// In progress panel - one request, two renderings, which is the point. Two
// independent views of the same work drift apart, and that is exactly how an
// import came to sit at 100% after it had finished.
//
// Nothing here drives the work. The build lives in the API process, survives the
// browser closing, and gives way to any render rather than competing with it.

import { ApiError, apiGet, apiSend } from './api'
import { element } from './ui'

const KINDS = ['place', 'poi'] as const
type Kind = (typeof KINDS)[number]

const IDLE_POLL_MS = 5000
const BUSY_POLL_MS = 1000

interface KindState {
  label: string
  zoom: number
  built: boolean
  names: number
  state: string
  tiles_done: number
  tiles_total: number
  percent: number
  rows_written: number
  duplicates: number
  built_from: string
  stale: boolean
  finished_at: string
  error: string
}

interface Status {
  archive: string
  kinds: Record<string, KindState>
  live: {
    kind: string
    state: string
    tiles_done: number
    tiles_total: number
    percent: number
    rows: number
    duplicates: number
    yielded: boolean
    message: string
    error: string
  }
  busy: boolean
}

export class Gazetteer {
  private readonly onBuilt: () => void
  private timer: number | undefined
  private watching = false
  private wasBusy = false

  constructor(onBuilt: () => void) {
    this.onBuilt = onBuilt
  }

  wire(): void {
    for (const kind of KINDS) {
      element(`gaz-${kind}-build`).addEventListener('click', () => void this.build(kind))
      element(`gaz-${kind}-stop`).addEventListener('click', () => void this.stop())
      element(`gaz-${kind}-delete`).addEventListener('click', () => void this.remove(kind))
    }
  }

  /** Called when a panel that shows any of this becomes visible, or stops being. */
  watch(on: boolean): void {
    this.watching = on
    if (this.timer !== undefined) {
      window.clearTimeout(this.timer)
      this.timer = undefined
    }
    if (on) void this.tick()
  }

  private async tick(): Promise<void> {
    if (!this.watching) return

    const status = await this.read()
    if (status) this.paint(status)

    const busy = Boolean(status?.busy)
    // A build that has just finished is the moment the search gets wider, so
    // whatever is on screen is worth refreshing.
    if (this.wasBusy && !busy) {
      this.wasBusy = false
      this.onBuilt()
    }
    if (busy) this.wasBusy = true

    this.timer = window.setTimeout(
      () => void this.tick(),
      busy ? BUSY_POLL_MS : IDLE_POLL_MS,
    )
  }

  private async read(): Promise<Status | null> {
    try {
      return await apiGet<Status>('/api/gazetteer', { timeoutMs: 15000 })
    } catch {
      return null
    }
  }

  private async build(kind: Kind): Promise<void> {
    // Said before it starts rather than after, because for points of interest
    // the answer to "how long" is hours and that is worth knowing first.
    if (kind === 'poi') {
      const sure = window.confirm(
        'Reading points of interest means going through the whole 137 GB ' +
          'archive: hours of work and several gigabytes of index.\n\n' +
          'It runs in the background, gives way to drawing and importing, and ' +
          'can be stopped and resumed. Start it?',
      )
      if (!sure) return
    }

    try {
      const answer = await apiSend<Status & { started?: boolean; reason?: string }>(
        'POST',
        `/api/gazetteer/${kind}`,
      )
      if (answer && answer.started === false && answer.reason) {
        this.say(kind, answer.reason, true)
      }
      if (answer) this.paint(answer)
    } catch (error) {
      this.say(kind, error instanceof ApiError ? error.message : String(error), true)
    }
    void this.tick()
  }

  private async stop(): Promise<void> {
    try {
      const answer = await apiSend<Status>('POST', '/api/gazetteer/stop')
      if (answer) this.paint(answer)
    } catch {
      /* the next poll says what happened */
    }
    void this.tick()
  }

  private async remove(kind: Kind): Promise<void> {
    const sure = window.confirm(
      `Delete the ${kind === 'place' ? 'place names' : 'points of interest'} ` +
        'read out of the basemap? Searching for them is switched off with it, ' +
        'and reading them again means another pass over the archive.',
    )
    if (!sure) return

    try {
      const answer = await apiSend<Status>('DELETE', `/api/gazetteer/${kind}`)
      if (answer) this.paint(answer)
      this.onBuilt()
    } catch (error) {
      this.say(kind, error instanceof ApiError ? error.message : String(error), true)
    }
    void this.tick()
  }

  private paint(status: Status): void {
    for (const kind of KINDS) {
      const about = status.kinds?.[kind]
      if (!about) continue

      const building = status.busy && status.live?.kind === kind
      const live = status.live

      element(`gaz-${kind}-built`).textContent = about.built
        ? about.finished_at ? `yes, ${about.finished_at.slice(0, 10)}` : 'yes'
        : 'not yet'
      element(`gaz-${kind}-names`).textContent = about.built
        ? about.names.toLocaleString()
        : '—'
      element(`gaz-${kind}-from`).textContent = about.built_from
        ? about.built_from.split(' ')[0] + (about.stale ? ' — the basemap has changed since' : '')
        : '—'

      const line = element(`gaz-${kind}-state`)
      if (about.error) {
        line.textContent = `It stopped: ${about.error}`
        line.dataset.state = 'bad'
      } else if (building) {
        line.textContent = live.yielded
          ? 'Waiting for the map to finish drawing — it gives way rather than competing.'
          : `Reading — ${live.percent}% of ${live.tiles_total.toLocaleString()} tiles, ` +
            `${live.rows.toLocaleString()} names so far.`
        line.dataset.state = 'good'
      } else if (about.state === 'stopping') {
        line.textContent =
          `Stopped part way, ${about.tiles_done.toLocaleString()} of ` +
          `${about.tiles_total.toLocaleString()} tiles read. Reading again carries on from there.`
        line.dataset.state = 'warn'
      } else if (about.stale) {
        line.textContent = 'The basemap has been replaced since this was read. Read it again to catch up.'
        line.dataset.state = 'warn'
      } else {
        line.textContent = ''
        line.dataset.state = ''
      }

      element(`gaz-${kind}-row`).hidden = !building
      if (building) {
        const bar = element<HTMLProgressElement>(`gaz-${kind}-bar`)
        bar.max = live.tiles_total || 100
        bar.value = live.tiles_done
        element(`gaz-${kind}-text`).textContent =
          `${live.tiles_done.toLocaleString()} of ${live.tiles_total.toLocaleString()} tiles — ` +
          `${live.duplicates.toLocaleString()} repeats dropped. This carries on if you close the browser.`
      }

      element<HTMLButtonElement>(`gaz-${kind}-build`).disabled = status.busy
      element<HTMLButtonElement>(`gaz-${kind}-build`).textContent =
        about.state === 'stopping' ? 'Carry on reading' : 'Read the archive'
      element<HTMLButtonElement>(`gaz-${kind}-stop`).disabled = !building
      element<HTMLButtonElement>(`gaz-${kind}-delete`).disabled =
        status.busy || (!about.built && about.state !== 'stopping')
    }

    this.paintProgressPanel(status)
  }

  /** The same poll, rendered as one line where all server work is watched. */
  private paintProgressPanel(status: Status): void {
    const line = element('gaz-progress')
    if (!status.busy) {
      line.hidden = true
      return
    }

    const live = status.live
    const label = status.kinds?.[live.kind]?.label ?? live.kind
    line.hidden = false
    line.dataset.state = 'good'
    line.textContent = live.yielded
      ? `${label}: waiting for this render to finish before carrying on.`
      : `${label}: reading the basemap — ${live.percent}% of ` +
        `${live.tiles_total.toLocaleString()} tiles, ${live.rows.toLocaleString()} names.`
  }

  private say(kind: Kind, message: string, bad = false): void {
    const line = element(`gaz-${kind}-state`)
    line.textContent = message
    line.dataset.state = bad ? 'bad' : ''
  }
}
