// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Running the deferred render, and following along while it works.
//
// Anything that changes what the tiles look like - a bulk import, the fog
// colour, the trail colours - marks the ground it affects as owing a render
// and returns. This is the other half: one request that does the work and
// reports each finished piece as it lands, so a minute of rendering is a
// minute of progress rather than a minute of nothing.

import { ApiError, getToken } from './api'

export interface RenderStep {
  done: number
  total: number
  finished?: boolean
  pending_tiles?: number
}

/**
 * A living estimate, rather than a percentage that lies.
 *
 * Jobs are wildly uneven - the cumulative view's dense tiles take many times
 * what a quiet year's do - so the completion rate is nothing like constant and
 * a bare percentage appears to stall and then leap. Projecting the remaining
 * jobs at the rate observed so far is self-correcting: it starts pessimistic,
 * settles as the mix evens out, and never claims to be nearly done when it is
 * not.
 */
export function estimate(step: RenderStep, startedAt: number): string {
  if (!step.total) return ''

  const percent = Math.round((step.done / step.total) * 100)
  const elapsed = (Date.now() - startedAt) / 1000
  if (step.done < 4 || elapsed < 5) return `${percent}%`

  const remaining = Math.round((elapsed / step.done) * (step.total - step.done))
  if (remaining < 45) return `${percent}%, under a minute left`
  return `${percent}%, about ${Math.max(1, Math.round(remaining / 60))} min left`
}

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
export async function runRender(report: (step: RenderStep) => void): Promise<void> {
  lockRenderControls(true)
  try {
    await stream(report)
  } finally {
    lockRenderControls(false)
  }
}

async function stream(report: (step: RenderStep) => void): Promise<void> {
  const response = await fetch('/api/render', {
    method: 'POST',
    headers: { 'X-Irfaran-Token': getToken() },
  })
  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}`)
  }
  if (!response.body) return

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const consume = (line: string) => {
    if (!line.trim()) return
    report(JSON.parse(line) as RenderStep)
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
