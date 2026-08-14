// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Live tracking sources. All off by default and independently toggleable -
// FogMap is entirely usable with none of them on, so nothing here is allowed
// to imply they are required.

import { ApiError, apiGet, apiSend } from './api'
import { element } from './ui'

const LABELS: Record<string, string> = {
  overland: 'Overland',
  owntracks: 'OwnTracks',
  ha: 'Home Assistant',
}

const NOTES: Record<string, string> = {
  overland: 'Batched, buffers while offline. Recommended.',
  owntracks: 'One fix per request, iOS and Android.',
  ha: 'Coarse. Event driven, no interval, no retry.',
}

export interface SourceState {
  source: string
  enabled: boolean
  has_events: boolean
}

interface SettingsResponse {
  settings: Record<string, string>
  sources: SourceState[]
}

export class Sources {
  private states: SourceState[] = []

  /** Sources worth showing anywhere: on, or off but holding history. */
  get active(): SourceState[] {
    return this.states.filter((state) => state.enabled || state.has_events)
  }

  async load(): Promise<void> {
    try {
      const body = await apiGet<SettingsResponse>('/api/settings')
      this.states = body.sources
    } catch {
      return
    }
    this.render()
  }

  private say(message: string, bad = false): void {
    const box = element('source-message')
    box.textContent = message
    box.hidden = !message
    box.dataset.state = bad ? 'bad' : ''
  }

  private async toggle(source: string, enabled: boolean): Promise<void> {
    try {
      await apiSend('PATCH', '/api/settings', {
        [`${source}_ingest_enabled`]: enabled ? 'true' : 'false',
      })
      this.say(`${LABELS[source] ?? source} ${enabled ? 'enabled' : 'disabled'}`)
      await this.load()
    } catch (error) {
      // Put the checkbox back where it was: nothing changed on the server.
      this.say(error instanceof ApiError ? error.message : String(error), true)
      await this.load()
    }
  }

  private render(): void {
    const box = element('source-list')
    box.replaceChildren()

    for (const state of this.states) {
      const row = document.createElement('label')
      row.className = 'source-row'

      const checkbox = document.createElement('input')
      checkbox.type = 'checkbox'
      checkbox.checked = state.enabled
      checkbox.dataset.source = state.source
      checkbox.addEventListener('change', () => {
        void this.toggle(state.source, checkbox.checked)
      })

      const text = document.createElement('span')
      const name = document.createElement('strong')
      name.textContent = LABELS[state.source] ?? state.source

      const note = document.createElement('small')
      note.textContent = state.has_events
        ? `${NOTES[state.source] ?? ''} Has recorded data.`
        : (NOTES[state.source] ?? '')

      text.append(name, document.createElement('br'), note)
      row.append(checkbox, text)
      box.append(row)
    }
  }
}
