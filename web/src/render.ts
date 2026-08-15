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
 * Render whatever is owed, calling `report` as it goes.
 *
 * The response is newline-delimited JSON rather than one object at the end,
 * which is the only way a progress bar can mean anything during work that
 * takes longer than a moment.
 */
export async function runRender(report: (step: RenderStep) => void): Promise<void> {
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

/** A percentage for a status line, or nothing while the total is unknown. */
export function percentOf(step: RenderStep): string {
  if (!step.total) return ''
  return ` ${Math.round((step.done / step.total) * 100)}%`
}
