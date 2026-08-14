// SPDX-License-Identifier: AGPL-3.0-or-later
//
// First-run setup. FogMap works without a basemap - fog and trails still
// render - so this never blocks the app, it just makes the one thing that
// cannot be shipped in a container easy to fetch.

import {
  ApiError,
  apiGet,
  apiSend,
  formatBytes,
  formatDuration,
  getToken,
  setToken,
} from './api'

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
    this.render(status)

    // Keep polling while a download runs even with the screen closed, so the
    // panel stays live.
    if (busy) this.poll()

    if (status.basemap.present || dismissed() || busy) return

    this.open()
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

    // Start it, then get out of the way. The download runs in the api
    // container, so closing this screen - or the browser - does not stop it.
    element('setup-background').addEventListener('click', () => {
      void this.start().then(() => {
        dismiss()
        this.close()
      })
    })

    // Pausing keeps the partial file. Cancelling throws it away, which after
    // several hours of downloading takes two clicks.
    for (const id of ['setup-cancel', 'basemap-pause']) {
      element(id).addEventListener('click', () => void this.cancel(false))
    }
    element('basemap-resume').addEventListener('click', () => void this.start())
    element('basemap-cancel').addEventListener('click', () => void this.confirmCancel())

    element('setup-skip').addEventListener('click', () => {
      dismiss()
      this.close()
    })
    element('setup-open').addEventListener('click', () => {
      this.open()
      void this.refresh()
      this.poll()
    })
    element('basemap-update').addEventListener('click', () => void this.update())
    element<HTMLInputElement>('setup-token').addEventListener('change', (event) => {
      setToken((event.target as HTMLInputElement).value.trim())
    })
  }

  /**
   * Re-download the basemap, for a newer planet build or a corrupt archive.
   *
   * Two clicks, because it is 128 GB. The existing archive keeps serving the
   * map throughout: the new one lands in a .part file and only replaces it
   * once it has been downloaded and checked.
   */
  private async update(): Promise<void> {
    const button = element<HTMLButtonElement>('basemap-update')

    if (button.dataset.armed !== 'true') {
      button.dataset.armed = 'true'
      button.textContent = 'Re-download 128 GB?'
      window.setTimeout(() => {
        button.dataset.armed = 'false'
        button.textContent = 'Update basemap'
      }, 5000)
      return
    }

    button.dataset.armed = 'false'
    button.textContent = 'Update basemap'
    await this.start()
    this.poll()
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
      // Already running is not a failure here: both the background button and
      // the panel are ways of saying "get on with it", and it already is.
      const message = error instanceof ApiError ? error.message : String(error)
      if (!message.includes('already running')) {
        this.say(message)
        throw error
      }
      this.poll()
    }
  }

  private async cancel(discard: boolean): Promise<void> {
    try {
      await apiSend(
        'DELETE',
        `/api/setup/basemap${discard ? '?discard=true' : ''}`,
        undefined,
        { tokenOptional: true },
      )
      await this.refresh()
    } catch (error) {
      this.say(error instanceof Error ? error.message : String(error))
    }
  }

  /** Discarding a part-finished download throws away hours of work. */
  private async confirmCancel(): Promise<void> {
    const button = element<HTMLButtonElement>('basemap-cancel')
    if (button.dataset.armed !== 'true') {
      button.dataset.armed = 'true'
      button.textContent = 'Discard progress?'
      window.setTimeout(() => {
        button.dataset.armed = 'false'
        button.textContent = 'Cancel'
      }, 5000)
      return
    }
    button.dataset.armed = 'false'
    button.textContent = 'Cancel'
    await this.cancel(true)
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

    // An update runs with the previous archive still installed, so "present"
    // alone is not a reason to stop watching - only a settled download is.
    const state = status.basemap.download.state
    if (state === 'running') return

    this.stopPolling()
    if (state === 'done' || status.basemap.present) {
      this.onBasemapReady()
    }
  }

  /** The same picture, in the settings panel, for when the screen is closed. */
  private renderPanel(status: SetupStatus): void {
    const { basemap } = status
    const download = basemap.download
    const running = download.state === 'running'

    const status_ = element('basemap-status')
    if (running) {
      status_.textContent =
        `Downloading ${formatBytes(download.downloaded_bytes)} of ` +
        `${formatBytes(download.total_bytes)}, ` +
        `${formatDuration(download.seconds_remaining)} remaining.`
      status_.dataset.state = 'warn'
    } else if (basemap.present) {
      status_.textContent = `Installed, ${formatBytes(basemap.bytes)}.`
      status_.dataset.state = 'good'
    } else if (basemap.partial_bytes) {
      status_.textContent = `Paused at ${formatBytes(basemap.partial_bytes)}.`
      status_.dataset.state = 'warn'
    } else {
      status_.textContent = 'Not installed.'
      status_.dataset.state = 'bad'
    }

    element('basemap-progress-row').hidden = !running
    element<HTMLProgressElement>('basemap-progress').value = download.percent
    element('basemap-progress-text').textContent = running
      ? `${download.percent.toFixed(1)}% · ${formatBytes(download.bytes_per_second)}/s`
      : ''

    const paused = !running && basemap.partial_bytes > 0

    element('basemap-pause').hidden = !running
    element('basemap-resume').hidden = !paused
    element('basemap-cancel').hidden = !running && !paused
    element('basemap-update').hidden = running || paused
  }

  private render(status: SetupStatus): void {
    this.renderPanel(status)

    const { basemap } = status
    const download = basemap.download
    const running = download.state === 'running'

    element('setup-background').hidden = basemap.present

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
