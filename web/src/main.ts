// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Phase 0 shell. Its only job is to make the running version visible, so the
// version in the browser and the version in the repository can be compared in
// two seconds when something looks wrong.

const REPO_URL = 'https://github.com/n0ne117/FogMap'

const webVersion = __FOGMAP_VERSION__

function element<T extends HTMLElement>(id: string): T {
  const found = document.getElementById(id)
  if (!found) {
    throw new Error(
      `FogMap web is missing the element #${id}. index.html and main.ts are out of sync.`,
    )
  }
  return found as T
}

function showWebVersion(): void {
  element('web-version').textContent = webVersion

  const corner = element<HTMLAnchorElement>('version-corner')
  corner.textContent = `v${webVersion}`
  corner.href = `${REPO_URL}/releases/tag/v${webVersion}`
  corner.title = `FogMap ${webVersion} - release notes`
}

async function showApiVersion(): Promise<void> {
  const target = element('api-version')

  try {
    const response = await fetch('/healthz', { headers: { accept: 'application/json' } })
    if (!response.ok) {
      target.textContent = `unreachable, http ${response.status}`
      target.dataset.state = 'bad'
      return
    }

    const body = (await response.json()) as { status?: string; version?: string }
    const apiVersion = body.version ?? 'unknown'
    target.textContent = apiVersion

    const matches = apiVersion === webVersion
    target.dataset.state = matches ? 'good' : 'warn'
    if (!matches) {
      target.textContent = `${apiVersion} - does not match this web build`
    }
  } catch (error) {
    target.textContent = 'unreachable'
    target.dataset.state = 'bad'
    console.error('FogMap could not reach the api at /healthz', error)
  }
}

showWebVersion()
void showApiVersion()
