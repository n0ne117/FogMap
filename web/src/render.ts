// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Watching the render queue, which belongs to the server.
//
// Anything that changes what the tiles look like - a bulk import, the fog
// colour, a tracker sync - marks the ground it affects as owing a render. The
// queue in the API process pays that debt, and everything here is a spectator:
// one request to start it, and polling to see how it is getting on.
//
// It used to be driven from here, by a request that streamed its progress - so
// the work advanced only while a browser held the response open, and closing a
// tab stopped it mid-pyramid. Nothing in this file can stop a render now, which
// is the point.

import { ApiError, apiGet, apiSend } from './api'

/**
 * Everything that must not be touched while tiles are being rewritten.
 *
 * Starting a second render over a half-finished one leaves the pyramid in a
 * state neither of them intended, so the controls that can start one are shut
 * off until the first has finished.
 */
const LOCKED = [
  'trail-ramp',
  'fog-colour',
  'fog-colour-hex',
  'fog-colour-apply',
  'import-button',
]

export function lockRenderControls(locked: boolean): void {
  for (const id of LOCKED) {
    const node = document.getElementById(id)
    if (!node) continue
    node.toggleAttribute('inert', locked)
    node.dataset.locked = String(locked)
    for (const control of node.querySelectorAll<HTMLButtonElement>('button, input')) {
      control.disabled = locked
    }
    if (node instanceof HTMLButtonElement || node instanceof HTMLInputElement) {
      node.disabled = locked
    }
  }
}

/**
 * Render whatever is owed, calling `report` as it goes.
 *
 * The response is newline-delimited JSON rather than one object at the end,
 * which is the only way a progress bar can mean anything during work that
 * takes longer than a moment.
 */
export interface RenderState {
  state: 'idle' | 'running' | 'stopping' | 'failed'
  done: number
  total: number
  percent: number
  elapsed_seconds: number
  seconds_remaining: number | null
  tiles_written: number
  rendering_views: string[]
  message: string
  error: string
  pending_tiles: number
  jobs: number
  jobs_done: number
  jobs_remaining: number
  pending_views: string[]
  seconds_per_job: number | null
  estimated_seconds: number | null
  can_start: boolean
  can_stop: boolean
}

/** How long a status poll may take before it is treated as broken. */
const POLL_TIMEOUT_MS = 15000

/** Consecutive failed polls tolerated before the watcher admits it lost track. */
const MAX_MISSED_POLLS = 5

/**
 * What the queue is doing, and what is still owed. Cheap enough to poll.
 *
 * With a deadline, because this is meant to be a few milliseconds and a poll
 * that hangs takes its caller with it - which is how an import that had finished
 * rendering sat at 100% forever, still waiting on one request.
 */
export async function renderState(): Promise<RenderState | null> {
  try {
    return await apiGet<RenderState>('/api/render', { timeoutMs: POLL_TIMEOUT_MS })
  } catch {
    return null
  }
}

/** Ask the server to start drawing. Returns as soon as it has begun. */
export async function startRender(): Promise<RenderState | null> {
  try {
    return await apiSend<RenderState>('POST', '/api/render')
  } catch (error) {
    if (error instanceof ApiError) throw error
    return null
  }
}

/** Ask it to stop after the tile in hand. Nothing already done is discarded. */
export async function stopRender(): Promise<RenderState | null> {
  try {
    return await apiSend<RenderState>('POST', '/api/render/stop')
  } catch {
    return null
  }
}

/**
 * What the render will cost, in words, before committing to waiting for it.
 *
 * Reads the same state as everything else: the queue knows the job count from
 * the pending tiles, and the rate from renders already measured on this
 * machine. Until one has been measured it gives the size and no duration,
 * because inventing one is worse than admitting there is nothing to go on.
 */
export function describeCost(state: RenderState | null): string {
  if (!state || !state.jobs_remaining) return ''

  const jobs = state.jobs_remaining
  const pieces = `${jobs.toLocaleString()} ${jobs === 1 ? 'piece' : 'pieces'} of work`
  if (!state.estimated_seconds) return pieces

  const seconds = state.estimated_seconds
  if (seconds < 45) return `${pieces}, a few seconds`
  const minutes = Math.max(1, Math.round(seconds / 60))
  return `${pieces}, about ${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`
}

/** How long is left, in words, from whatever the queue last said. */
export function describeRemaining(state: RenderState): string {
  // Between passes the counters briefly agree while the next pass is being
  // worked out, and a bar reading 100% next to the word "drawing" is what a
  // hang looks like even when nothing is wrong.
  if (state.total && state.done >= state.total) return 'working out what is left'

  const seconds = state.seconds_remaining ?? state.estimated_seconds
  if (!seconds) return `${state.percent}%`
  if (seconds < 45) return `${state.percent}%, under a minute left`
  const minutes = Math.max(1, Math.round(seconds / 60))
  return `${state.percent}%, about ${minutes} min left`
}

/**
 * Follow a render to its end, reporting as it goes.
 *
 * Polling, not streaming, and that is the point: the render belongs to the
 * server, so this is a spectator. Closing the tab halfway through stops the
 * watching and nothing else - the queue carries on, and the next page load
 * picks the progress back up where it actually is.
 */
export async function watchRender(
  report: (state: RenderState) => void,
  everyMs = 700,
): Promise<RenderState | null> {
  let misses = 0

  for (;;) {
    const state = await renderState()

    if (!state) {
      // A failed poll used to end the watch and hand back the last state seen,
      // so a single blip during a long render reported it as finished. A few
      // are worth riding out; past that, say nothing rather than something
      // untrue - null means "lost track", not "done".
      misses += 1
      if (misses > MAX_MISSED_POLLS) return null
      await new Promise((wake) => window.setTimeout(wake, everyMs * 2))
      continue
    }

    misses = 0
    report(state)

    if (state.state !== 'running' && state.state !== 'stopping') return state
    await new Promise((wake) => window.setTimeout(wake, everyMs))
  }
}

/** Start it if it is not going, then watch. What a caller almost always wants. */
export async function runRender(
  report: (state: RenderState) => void,
): Promise<RenderState | null> {
  lockRenderControls(true)
  try {
    const begun = await startRender()
    if (begun) report(begun)
    return await watchRender(report)
  } finally {
    lockRenderControls(false)
  }
}

export async function readNdjson(
  response: Response,
  onLine: (value: Record<string, unknown>) => void,
): Promise<void> {
  if (!response.body) return

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const consume = (line: string) => {
    if (!line.trim()) return
    onLine(JSON.parse(line) as Record<string, unknown>)
  }

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() ?? ''
    for (const line of lines) consume(line)
  }
  consume(buffer)
}
