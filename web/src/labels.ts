// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The label editor, in settings.
//
// Labels live here rather than in the places sidebar because they are a
// setting: defined once, used by however many pins. The sidebar is for the
// pins themselves, and mixing the two put the rarely-used thing in the way of
// the constantly-used one.

import { ApiError, apiGet, apiSend } from './api'
import { icon } from './icons'
import { element } from './ui'
import type { Label } from './places'

const DEFAULT_COLOUR = '#4d8fd6'

export class Labels {
  private readonly onChanged: () => void
  private labels: Label[] = []

  constructor(onChanged: () => void) {
    this.onChanged = onChanged
  }

  wire(): void {
    const wheel = element<HTMLInputElement>('label-colour')
    const hex = element<HTMLInputElement>('label-colour-hex')

    wheel.addEventListener('input', () => (hex.value = wheel.value))
    hex.addEventListener('change', () => {
      const value = hex.value.trim()
      if (/^#?[0-9a-fA-F]{6}$/.test(value)) {
        wheel.value = value.startsWith('#') ? value : `#${value}`
      }
    })

    element('label-add').addEventListener('click', () => void this.add())
    element<HTMLInputElement>('label-name').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') void this.add()
    })
  }

  async load(): Promise<void> {
    try {
      const body = await apiGet<{ labels: Label[] }>('/api/labels')
      this.labels = body.labels ?? []
      this.say('')
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    this.paint()
  }

  private async add(): Promise<void> {
    const name = element<HTMLInputElement>('label-name').value.trim()
    if (!name) {
      this.say('A label needs a name.', true)
      return
    }

    try {
      await apiSend('POST', '/api/labels', {
        name,
        colour: element<HTMLInputElement>('label-colour').value,
      })
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    element<HTMLInputElement>('label-name').value = ''
    element<HTMLInputElement>('label-colour').value = DEFAULT_COLOUR
    element<HTMLInputElement>('label-colour-hex').value = ''

    await this.load()
    this.onChanged()
  }

  private async change(label: Label, changes: Record<string, unknown>): Promise<void> {
    try {
      await apiSend('PATCH', `/api/labels/${label.id}`, changes)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      await this.load()
      return
    }
    await this.load()
    this.onChanged()
  }

  private async remove(label: Label): Promise<void> {
    const ok = window.confirm(
      `Delete the label "${label.name}"? Pins using it keep their place and ` +
        'only lose the colour.',
    )
    if (!ok) return

    try {
      await apiSend('DELETE', `/api/labels/${label.id}`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
    this.onChanged()
  }

  private paint(): void {
    const list = element('label-list')
    list.replaceChildren()
    element('label-empty').hidden = this.labels.length > 0

    for (const label of this.labels) {
      const row = document.createElement('div')
      row.className = 'label-row'

      const colour = document.createElement('input')
      colour.type = 'color'
      colour.value = label.colour
      colour.setAttribute('aria-label', `Colour for ${label.name}`)
      colour.addEventListener('change', () => void this.change(label, { colour: colour.value }))

      const name = document.createElement('input')
      name.type = 'text'
      name.value = label.name
      name.setAttribute('aria-label', `Name of ${label.name}`)
      name.addEventListener('change', () => {
        const renamed = name.value.trim()
        if (renamed && renamed !== label.name) void this.change(label, { name: renamed })
      })

      const remove = document.createElement('button')
      remove.type = 'button'
      remove.append(icon('trash'))
      remove.title = `Delete ${label.name}`
      remove.setAttribute('aria-label', remove.title)
      remove.addEventListener('click', () => void this.remove(label))

      row.append(colour, name, remove)
      list.append(row)
    }
  }

  private say(message: string, bad = false): void {
    const line = element('label-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}
