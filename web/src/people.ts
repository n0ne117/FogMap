// SPDX-License-Identifier: AGPL-3.0-or-later
//
// The who-was-there registry, in settings.
//
// Here rather than in the sidebar for the same reason labels are: it is a
// setting, written once and used by however many pins. The sidebar is for the
// pins.
//
// The point of registering names at all is that "Who?" on a pin can then be
// multiple choice. Free text would ask you to spell somebody the same way
// every time and would make "everywhere I went with Marie" unanswerable.

import { ApiError, apiGet, apiSend } from './api'
import { icon } from './icons'
import { element } from './ui'
import type { Person } from './places'

export class People {
  private readonly onChanged: () => void
  private people: Person[] = []
  private namedOnPins: string[] = []

  constructor(onChanged: () => void) {
    this.onChanged = onChanged
  }

  wire(): void {
    element('person-add').addEventListener('click', () => void this.add())
    element<HTMLInputElement>('person-name').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') void this.add()
    })
  }

  async load(): Promise<void> {
    try {
      const body = await apiGet<{ people: Person[]; named_on_pins: string[] }>(
        '/api/people',
      )
      this.people = body.people ?? []
      this.namedOnPins = body.named_on_pins ?? []
      this.say('')
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    this.paint()
  }

  private async add(): Promise<void> {
    const field = element<HTMLInputElement>('person-name')
    const name = field.value.trim()
    if (!name) {
      this.say('A name is a name.', true)
      return
    }

    try {
      await apiSend('POST', '/api/people', { name })
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }

    field.value = ''
    await this.load()
    this.onChanged()
  }

  private async rename(person: Person, name: string): Promise<void> {
    try {
      await apiSend('PATCH', `/api/people/${person.id}`, { name })
      this.say(`Renamed on the list and on every pin naming ${person.name}.`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
    }
    await this.load()
    this.onChanged()
  }

  private async remove(person: Person): Promise<void> {
    const ok = window.confirm(
      `Take ${person.name} off the list? Pins that record them keep the name — ` +
        'this only stops it being offered.',
    )
    if (!ok) return

    try {
      await apiSend('DELETE', `/api/people/${person.id}`)
    } catch (error) {
      this.say(error instanceof ApiError ? error.message : String(error), true)
      return
    }
    await this.load()
    this.onChanged()
  }

  private paint(): void {
    const list = element('person-list')
    list.replaceChildren()
    element('person-empty').hidden = this.people.length > 0

    for (const person of this.people) {
      const row = document.createElement('div')
      row.className = 'label-row'

      const name = document.createElement('input')
      name.type = 'text'
      name.value = person.name
      name.setAttribute('aria-label', `Name of ${person.name}`)
      name.addEventListener('change', () => {
        const renamed = name.value.trim()
        if (renamed && renamed !== person.name) void this.rename(person, renamed)
        else name.value = person.name
      })

      const remove = document.createElement('button')
      remove.type = 'button'
      remove.append(icon('trash'))
      remove.title = `Take ${person.name} off the list`
      remove.setAttribute('aria-label', remove.title)
      remove.addEventListener('click', () => void this.remove(person))

      row.append(name, remove)
      list.append(row)
    }

    // Somebody on a pin but not on the list: from an older backup, or removed
    // from the list after the pin was made. Worth saying, because they show up
    // as a choice on a pin and would otherwise look like a bug.
    const registered = new Set(this.people.map((person) => person.name))
    const strays = this.namedOnPins.filter((name) => !registered.has(name))
    if (strays.length) {
      const note = document.createElement('p')
      note.className = 'hint'
      note.textContent =
        `Also named on pins, but not on this list: ${strays.join(', ')}. ` +
        'They stay offered on any pin that already names them.'
      list.append(note)
    }
  }

  private say(message: string, bad = false): void {
    const line = element('person-message')
    line.textContent = message
    line.hidden = !message
    line.dataset.state = bad ? 'bad' : ''
  }
}
