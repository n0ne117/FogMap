// SPDX-License-Identifier: AGPL-3.0-or-later
//
// First-run setup. Irfaran works without a basemap - fog and trails still
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
import { copyText, element } from './ui'

/** Whatever the interface theme is set to, so the check writes back a no-op. */
function getUiThemeSetting(): string {
  try {
    return window.localStorage.getItem('irfaran.ui.theme') ?? 'system'
  } catch {
    return 'system'
  }
}

const DISMISSED_KEY = 'irfaran.setup.dismissed'

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
  completed: boolean
  /** `value` is present only during genuine first-run setup. */
  token: { value?: string; source: 'environment' | 'generated' }
  basemap: {
    filename: string
    present: boolean
    bytes: number
    partial_bytes: number
    path: string
    download: DownloadStatus
  }
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

  private completed = false

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

    this.completed = status.completed
    this.fillSources(status)

    // Adopt the server's token automatically. Nobody should have to copy a
    // secret from one part of the interface into another on the same machine.
    // The server only offers it during first-run setup.
    if (status.token?.value && status.token.value !== getToken()) {
      setToken(status.token.value)
    }

    const busy = status.basemap.download.state === 'running'
    this.render(status)

    // Keep polling while a download runs even with the screen closed, so the
    // panel stays live.
    if (busy) this.poll()

    // Two different screens, and two different reasons to show them.
    //
    // Before setup is finished this is where the token is handed over, and
    // that has to be seen once. After it, a browser that already has a working
    // token has no business being interrupted - but one that does not is about
    // to find every edit refused, so it gets told why.
    if (dismissed()) return
    if (status.completed && getToken()) return

    this.open()
    if (!busy) this.poll()
  }

  open(): void {
    element('setup').hidden = false
  }

  /**
   * Done with setup: stop showing it here, and tell the server.
   *
   * Telling the server is what stops the token being served to whoever asks,
   * so it has to happen the moment somebody has been given it - not on some
   * later action they might never take. Best effort: without a token the call
   * is refused, which is correct, and setup simply stays open to be finished
   * by someone who has one.
   */
  private async finish(): Promise<void> {
    dismiss()
    this.close()
    if (!this.completed && getToken()) {
      try {
        await apiSend('POST', '/api/setup/complete')
        this.completed = true
      } catch {
        /* the next visitor will be offered it again, which is the safe way
           for this to fail */
      }
    }
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
        void this.finish()
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
      void this.finish()
    })
    element('setup-open').addEventListener('click', () => {
      this.open()
      void this.refresh()
      this.poll()
    })
    element('basemap-update').addEventListener('click', () => void this.update())

    element('setup-continue').addEventListener('click', () => {
      void this.finish()
    })

    element('setup-token-copy').addEventListener('click', () => {
      void this.copyToken()
    })

    // The already-set-up screen: this browser has to be given the token,
    // because the server will not hand it out any more.
    element('setup-known-save').addEventListener('click', () => void this.adopt())
    element<HTMLInputElement>('setup-known-token').addEventListener(
      'keydown',
      (event) => {
        if (event.key === 'Enter') void this.adopt()
      },
    )
    element('setup-known-skip').addEventListener('click', () => {
      void this.finish()
    })
  }

  /** Copy the token to the clipboard, on plain http as well.
   *
   * navigator.clipboard only exists in a secure context, which a self-hosted
   * box reached at http://tower:8080 is not. The button used to optional-chain
   * straight past that and do nothing whatsoever - no copy, no complaint - on
   * exactly the setup this project is built for. The token is shown once, so
   * silently failing to copy it is about the worst moment for it.
   */
  private async copyToken(): Promise<void> {
    const button = element('setup-token-copy')
    const token = element('setup-token-value').textContent ?? ''
    const say = (message: string): void => {
      button.textContent = message
      window.setTimeout(() => (button.textContent = 'Copy'), 2500)
    }

    if (await copyText(token)) {
      say('Copied')
      return
    }

    // Nothing could write to the clipboard, so the token is selected instead
    // and Ctrl+C finishes the job. Better than "Copy" quietly doing nothing
    // and the token scrolling away unread.
    const range = document.createRange()
    range.selectNodeContents(element('setup-token-value'))
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    say('Selected — press Ctrl+C')
  }

  /**
   * Take a pasted token, and prove it works before believing it.
   *
   * Storing whatever was typed and finding out at the first edit is how you
   * end up with somebody certain they entered it correctly and an app that
   * disagrees silently.
   */
  private async adopt(): Promise<void> {
    const field = element<HTMLInputElement>('setup-known-token')
    const message = element('setup-known-message')
    const value = field.value.trim()

    const say = (text: string, bad = false) => {
      message.textContent = text
      message.hidden = !text
      message.dataset.state = bad ? 'bad' : ''
    }

    if (!value) {
      say('Paste the token first.', true)
      return
    }

    const previous = getToken()
    setToken(value)
    say('Checking.')
    try {
      // Harmless either way: it writes back the theme it already has.
      await apiSend('PATCH', '/api/settings', { ui_theme: getUiThemeSetting() })
    } catch (error) {
      setToken(previous)
      say(
        error instanceof ApiError && error.status === 401
          ? 'That token is not the one this server is using.'
          : error instanceof ApiError
            ? error.message
            : String(error),
        true,
      )
      return
    }

    say('')
    field.value = ''
    void this.finish()
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

    // No token is read here. This screen used to have a field to type one
    // into; it now shows the token the server generated, and refresh() adopts
    // that for us. Reading a field that no longer exists threw before the
    // request was ever sent, which killed every button routing through here.
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

    // One card or the other, never both. The token card only ever appears
    // while the server is still willing to say what the token is.
    const offered = Boolean(status.token?.value)
    element('setup-token-card').hidden = !offered
    element('setup-known').hidden = offered || !status.completed

    if (offered) {
      element('setup-token-value').textContent = status.token.value ?? ''
      element('setup-token-source').textContent =
        status.token.source === 'environment'
          ? 'Set by IRFARAN_TOKEN in the server environment.'
          : 'Generated by this server on first start and stored with your data.'
    } else if (!status.completed) {
      // Setup is unfinished but there is nothing to reveal, which means the
      // operator chose the token themselves and already knows it.
      element('setup-known').hidden = false
    }

    // An archive already on disk means there is nothing to do here but leave.
    // No size, no "everything is ready", and no offer of another one: a
    // 137 GB download is not something to put a button for on the screen
    // somebody sees before they have looked at their map even once. Settings,
    // Basemap still has all of it for anyone who actually wants to swap.
    const settled = basemap.present && !running
    element('setup-ready').hidden = !settled
    if (settled) {
      element('setup-fetch').hidden = true
    } else if (element('setup-ready').hidden) {
      element('setup-fetch').hidden = false
    }

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

