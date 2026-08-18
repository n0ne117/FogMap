// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The History tab. What happened, newest first.
//
// Four categories, coloured by what they mean rather than by severity: errors
// red, things done by hand white, data that arrived on its own amber, and
// whatever the server did to itself grey. The colour is set from a data
// attribute so the palette lives in the stylesheet with every other colour.

import { ApiError, apiGet, apiSend } from './api'
import { element, radioGroup } from './ui'

type Category = '' | 'error' | 'manual' | 'source' | 'system'

interface Entry {
  id: number
  at: string
  category: string
  action: string
  message: string
  count: number
}

interface HistoryResponse {
  entries: Entry[]
  counts: Record<string, number>
  kept: { entries: number; days: number }
}

export class History {
  private filter: Category = ''
  private loaded = false

  wire(): void {
    radioGroup<Category>('history-filter', '', (value) => {
      this.filter = value
      void this.load()
    })

    element('history-refresh').addEventListener('click', () => void this.load())
    element('history-clear').addEventListener('click', () => void this.clear())
  }

  /** Load on first look rather than on startup: nobody has opened the tab yet. */
  async loadOnce(): Promise<void> {
    if (this.loaded) return
    this.loaded = true
    await this.load()
  }

  async load(): Promise<void> {
    try {
      const query = this.filter ? `?category=${this.filter}` : ''
      const body = await apiGet<HistoryResponse>(`/api/history${query}`)
      this.render(body)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
  }

  private render(body: HistoryResponse): void {
    const list = element('history-list')
    list.textContent = ''

    for (const entry of body.entries) {
      const row = document.createElement('div')
      row.className = 'history-row'
      row.dataset.category = entry.category

      const when = document.createElement('span')
      when.className = 'history-when'
      when.textContent = shortTime(entry.at)

      const what = document.createElement('span')
      what.className = 'history-what'
      what.textContent = entry.message

      row.append(when, what)

      // A coalesced line says how many times it happened, or it looks like one
      // delivery when it was forty.
      if (entry.count > 1) {
        const count = document.createElement('span')
        count.className = 'history-count'
        count.textContent = `×${entry.count}`
        row.append(count)
      }

      list.append(row)
    }

    element('history-empty').hidden = body.entries.length > 0
    list.hidden = body.entries.length === 0

    const total = Object.values(body.counts).reduce((sum, n) => sum + n, 0)
    element('history-kept').textContent =
      `${total} kept — the newest ${body.kept.entries} entries, ` +
      `nothing older than ${body.kept.days} days. ` +
      `${body.counts.error ?? 0} errors.`
  }

  private async clear(): Promise<void> {
    const button = element<HTMLButtonElement>('history-clear')
    if (button.dataset.armed !== 'true') {
      button.dataset.armed = 'true'
      button.textContent = 'Clear it all?'
      window.setTimeout(() => {
        button.dataset.armed = 'false'
        button.textContent = 'Clear history'
      }, 4000)
      return
    }

    button.dataset.armed = 'false'
    button.textContent = 'Clear history'
    try {
      const body = await apiSend<{ cleared: number }>('DELETE', '/api/history')
      this.say(`Forgot ${body.cleared} entries.`)
      await this.load()
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
  }

  private say(message: string, bad = false): void {
    const line = element('history-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}

/** Compact enough to sit in a column: today shows a time, older shows a date. */
function shortTime(stamp: string): string {
  const when = new Date(stamp)
  if (Number.isNaN(when.getTime())) return stamp

  const today = new Date()
  const sameDay =
    when.getFullYear() === today.getFullYear() &&
    when.getMonth() === today.getMonth() &&
    when.getDate() === today.getDate()

  return sameDay
    ? when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
    : when.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
        ' ' +
        when.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })
}
