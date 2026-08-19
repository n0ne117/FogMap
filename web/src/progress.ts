// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The In progress tab: what the server is drawing, and what it still owes.
//
// The render queue lives in the API process, so this panel is a window onto it
// rather than the thing driving it. That distinction is the whole feature: you
// can start a render, close the browser, come back tomorrow, and this will tell
// you exactly how far it got - because the answer was never in the browser.
//
// Polls only while the panel is open. There is no point asking every second
// about a render nobody is looking at, and a closed panel asking anyway is how
// a laptop fan gets loud for no reason.

import { ApiError } from './api'
import type { RenderState } from './render'
import { renderState, startRender, stopRender } from './render'
import { element } from './ui'

const IDLE_POLL_MS = 4000
const BUSY_POLL_MS = 800

export class Progress {
  private readonly onDrawn: () => void
  private timer: number | undefined
  private watching = false
  private wasRunning = false

  constructor(onDrawn: () => void) {
    this.onDrawn = onDrawn
  }

  wire(): void {
    element('progress-start').addEventListener('click', () => void this.start())
    element('progress-stop').addEventListener('click', () => void this.stop())
  }

  /** Called when the panel becomes visible, and when it stops being. */
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

    const state = await renderState()
    if (state) this.paint(state)

    const busy = state?.state === 'running' || state?.state === 'stopping'

    // A render that has just finished is the moment the map is worth redrawing.
    if (this.wasRunning && !busy) {
      this.wasRunning = false
      this.onDrawn()
    }
    if (busy) this.wasRunning = true

    this.timer = window.setTimeout(
      () => void this.tick(),
      busy ? BUSY_POLL_MS : IDLE_POLL_MS,
    )
  }

  private async start(): Promise<void> {
    this.say('')
    try {
      const begun = await startRender()
      if (begun && !(begun as unknown as { started?: boolean }).started) {
        const reason = (begun as unknown as { reason?: string }).reason
        if (reason === 'nothing pending') this.say('Nothing is waiting to be drawn.')
        else if (reason) this.say(`Already going: ${reason}.`)
      }
      if (begun) this.paint(begun)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
    void this.tick()
  }

  private async stop(): Promise<void> {
    this.say('')
    const stopped = await stopRender()
    if (stopped) this.paint(stopped)
    this.say(
      'Stopping once the tiles in hand are finished. Nothing already done is lost.',
    )
    void this.tick()
  }

  private paint(raw: RenderState): void {
    // Every number is read through a default. The server answers one shape from
    // all three render endpoints now, but a panel that throws on a missing
    // field takes the whole page down with it - and that is exactly what
    // happened: start and stop used to reply with the worker's snapshot alone,
    // paint read `undefined.toLocaleString()`, and the error landed on screen.
    const state: RenderState = {
      ...raw,
      done: raw.done ?? 0,
      total: raw.total ?? 0,
      tiles_written: raw.tiles_written ?? 0,
      pending_tiles: raw.pending_tiles ?? 0,
      jobs: raw.jobs ?? 0,
      jobs_done: raw.jobs_done ?? 0,
      jobs_remaining: raw.jobs_remaining ?? 0,
      pending_views: raw.pending_views ?? [],
    }

    const busy = state.state === 'running' || state.state === 'stopping'

    // One source for "how far along", used by the bar, the caption and the
    // table alike. There are two available - the worker's live counter and the
    // count of finished jobs on disk - and they differ by a job or two while
    // work is in flight. Showing both means showing a contradiction.
    const done = busy && state.total ? state.done : state.jobs_done
    const total = busy && state.total ? state.total : state.jobs
    const percent = total ? Math.round((100 * done) / total) : 0

    const line = element('progress-state')
    if (state.state === 'failed') {
      line.textContent = `The last render stopped: ${state.error}`
      line.dataset.state = 'bad'
    } else if (busy) {
      line.textContent =
        state.state === 'stopping'
          ? 'Stopping — waiting for the tiles already being drawn to finish, ' +
            `so none is left half written. ${done.toLocaleString()} of ` +
            `${total.toLocaleString()} done.`
          : `Drawing — ${percent}%${remainingWords(state)}.`
      line.dataset.state = 'good'
    } else if (state.pending_tiles) {
      line.textContent =
        `Not drawing. ${state.jobs_remaining.toLocaleString()} of ` +
        `${state.jobs.toLocaleString()} pieces of work still to do.`
      line.dataset.state = 'warn'
    } else {
      line.textContent = state.message || 'Everything is drawn.'
      line.dataset.state = ''
    }

    element('progress-row').hidden = !busy && !state.pending_tiles
    const bar = element<HTMLProgressElement>('progress-bar')
    if (total) {
      bar.max = total
      bar.value = done
    }
    element('progress-text').textContent = busy
      ? `${done.toLocaleString()} of ${total.toLocaleString()} — ` +
        `${state.tiles_written.toLocaleString()} tiles written this run`
      : state.pending_tiles
        ? 'Paused. Resume carries on from here rather than starting again.'
        : ''

    element('progress-pending').textContent = state.pending_tiles.toLocaleString()
    element('progress-done').textContent = total
      ? `${done.toLocaleString()} of ${total.toLocaleString()}`
      : '—'
    element('progress-left').textContent = state.jobs_remaining
      ? state.jobs_remaining.toLocaleString() +
        (state.estimated_seconds
          ? ` — about ${Math.max(1, Math.round(state.estimated_seconds / 60))} min`
          : '')
      : 'nothing'
    element('progress-views').textContent = state.pending_views.length
      ? state.pending_views.join(', ')
      : '—'
    element('progress-workers').textContent = String(
      (state as unknown as { workers?: number }).workers ?? '—',
    )

    element<HTMLButtonElement>('progress-start').disabled = !state.can_start
    element<HTMLButtonElement>('progress-stop').disabled = !state.can_stop
  }

  private say(message: string, bad = false): void {
    const line = element('progress-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}

/** ", about 4 min left", or nothing when there is no basis for saying. */
function remainingWords(state: RenderState): string {
  const seconds = state.seconds_remaining ?? state.estimated_seconds
  if (!seconds) return ''
  if (seconds < 45) return ', under a minute left'
  return `, about ${Math.max(1, Math.round(seconds / 60))} min left`
}
