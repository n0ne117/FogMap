// SPDX-License-Identifier: AGPL-3.0-or-later
//
// First-run setup. FogMap works without a basemap - fog and trails still
// render - so this never blocks the app, it just makes the one thing that
// cannot be shipped in a container easy to fetch.

import { apiGet, apiSend, formatBytes, formatDuration, getToken, setToken } from './api'

const DISMISSED_KEY = 'fogmap.setup.dismissed'

export interface DownloadStatus {
  url: string
  filename: string
  state: 'idle' | 'running' | 'done' | 'error' | 'cancelled'
  total_bytes: number
  downloaded_bytes: number
  percent: number
  bytes_per_second: number
  seconds_remaining: number | null
  error: string
}

export interface SetupStatus {
  version: string
  data_dir: string
  suggested_urls: string[]
  basemap: {
    filename: string
    present: boolean
    bytes: number
    partial_bytes: number
    path: string
    download: DownloadStatus
  }
}

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) throw new Error(`FogMap setup is missing the element #${id}.`)
  return found as T
}

function dismissed(): boolean {
  try {
    return window.localStorage.getItem(DISMISSED_KEY) === 'true'
  } catch {
    return false
  }
}

function dismiss(): void {
  try {
    window.localStorage.setItem(DISMISSED_KEY, 'true')
  } catch {
    /* nothing to do */
  }
}

export class Setup {
  private timer: number | undefined
  private onBasemapReady: () => void

  constructor(onBasemapReady: () => void) {
    this.onBasemapReady = onBasemapReady
  }

  /** Show the screen unless a basemap is already present or it was dismissed. */
  async maybeShow(): Promise<void> {
    let status: SetupStatus
    try {
      status = await apiGet<SetupStatus>('/api/setup')
    } catch {
      return
    }

    this.fillSources(status)
    element<HTMLInputElement>('setup-token').value = getToken()

    const busy = status.basemap.download.state === 'running'
    if (status.basemap.present || (dismissed() && !busy)) {
      this.render(status)
      return
    }

    this.open()
    this.render(status)
    this.poll()
  }

  open(): void {
    element('setup').hidden = false
  }

  close(): void {
    element('setup').hidden = true
  }

  wire(): void {
    element('setup-start').addEventListener('click', () => void this.start())
    element('setup-cancel').addEventListener('click', () => void this.cancel())
    element('setup-skip').addEventListener('click', () => {
      dismiss()
      this.close()
    })
    element('setup-open').addEventListener('click', () => {
      this.open()
      void this.refresh()
      this.poll()
    })
    element<HTMLInputElement>('setup-token').addEventListener('change', (event) => {
      setToken((event.target as HTMLInputElement).value.trim())
    })
  }

  private fillSources(status: SetupStatus): void {
    const select = element<HTMLSelectElement>('setup-url')
    if (select.options.length > 0) return
    for (const url of status.suggested_urls) {
      const option = document.createElement('option')
      option.value = url
      option.textContent = url.split('/').pop() ?? url
      select.append(option)
    }
  }

  private async start(): Promise<void> {
    const custom = element<HTMLInputElement>('setup-custom').value.trim()
    const url = custom || element<HTMLSelectElement>('setup-url').value
    setToken(element<HTMLInputElement>('setup-token').value.trim())

    this.say('')
    try {
      // The offered builds need no token; a custom URL does, and the
      // server says so if one is missing.
      await apiSend('POST', '/api/setup/basemap', { url }, { tokenOptional: true })
      this.poll()
    } catch (error) {
      this.say(error instanceof Error ? error.message : String(error))
    }
  }

  private async cancel(): Promise<void> {
    try {
      await apiSend('DELETE', '/api/setup/basemap', undefined, { tokenOptional: true })
      await this.refresh()
    } catch (error) {
      this.say(error instanceof Error ? error.message : String(error))
    }
  }

  private say(message: string): void {
    const box = element('setup-message')
    box.textContent = message
    box.hidden = !message
  }

  private poll(): void {
    window.clearInterval(this.timer)
    this.timer = window.setInterval(() => void this.refresh(), 2000)
  }

  private stopPolling(): void {
    window.clearInterval(this.timer)
    this.timer = undefined
  }

  async refresh(): Promise<void> {
    let status: SetupStatus
    try {
      status = await apiGet<SetupStatus>('/api/setup')
    } catch {
      return
    }
    this.render(status)

    const state = status.basemap.download.state
    if (state === 'done' || status.basemap.present) {
      this.stopPolling()
      this.onBasemapReady()
    } else if (state === 'error' || state === 'cancelled' || state === 'idle') {
      this.stopPolling()
    }
  }

  private render(status: SetupStatus): void {
    const { basemap } = status
    const download = basemap.download
    const running = download.state === 'running'

    element('setup-open').hidden = basemap.present && !running

    const summary = element('setup-state')
    if (basemap.present) {
      summary.textContent = `Basemap installed, ${formatBytes(basemap.bytes)}.`
      summary.dataset.state = 'good'
    } else if (running) {
      summary.textContent = 'Downloading.'
      summary.dataset.state = 'warn'
    } else if (basemap.partial_bytes) {
      summary.textContent =
        `Partial download of ${formatBytes(basemap.partial_bytes)} on disk. ` +
        'Starting again resumes from there.'
      summary.dataset.state = 'warn'
    } else {
      summary.textContent = 'No basemap installed.'
      summary.dataset.state = 'bad'
    }

    element('setup-progress-row').hidden = !running && download.state !== 'error'
    element<HTMLProgressElement>('setup-progress').value = download.percent
    element('setup-progress-text').textContent = running
      ? `${formatBytes(download.downloaded_bytes)} of ${formatBytes(
          download.total_bytes,
        )}  ·  ${download.percent.toFixed(1)}%  ·  ` +
        `${formatBytes(download.bytes_per_second)}/s  ·  ` +
        `${formatDuration(download.seconds_remaining)} remaining`
      : ''

    element('setup-start').hidden = running
    element('setup-cancel').hidden = !running

    if (download.state === 'error' && download.error) {
      this.say(download.error)
    }
  }
}
