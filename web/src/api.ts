// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Every mutating request carries the shared token. The browser keeps it in
// localStorage rather than the web container injecting it, so the token stays
// a real gate instead of something anyone reaching the page gets for free.

const TOKEN_KEY = 'irfaran.token'

export function getToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) {
      window.localStorage.setItem(TOKEN_KEY, token)
    } else {
      window.localStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    // Storage disabled. The token still works for this page load.
  }
}

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function parse(response: Response): Promise<unknown> {
  const body = await response.text()
  let parsed: unknown = body
  try {
    parsed = body ? JSON.parse(body) : null
  } catch {
    // A non-JSON error body is still worth showing verbatim.
  }

  if (!response.ok) {
    const detail =
      parsed && typeof parsed === 'object' && 'detail' in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `${response.status} ${response.statusText}`
    throw new ApiError(response.status, detail)
  }
  return parsed
}

export async function apiGet<T>(path: string): Promise<T> {
  return (await parse(await fetch(path, { headers: { accept: 'application/json' } }))) as T
}

export async function apiSend<T>(
  method: 'POST' | 'PATCH' | 'DELETE',
  path: string,
  body?: unknown,
  options: { tokenOptional?: boolean } = {},
): Promise<T> {
  const token = getToken()
  if (!token && !options.tokenOptional) {
    throw new ApiError(
      401,
      'No API token set. Put the value of IRFARAN_TOKEN into ' +
        'Settings, Data sources, API token.',
    )
  }

  const response = await fetch(path, {
    method,
    headers: {
      // Sent when there is one. Some routes decide for themselves whether
      // they need it, so refusing here would pre-empt the server's answer.
      ...(token ? { 'X-Irfaran-Token': token } : {}),
      accept: 'application/json',
      ...(body === undefined ? {} : { 'content-type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  return (await parse(response)) as T
}

export function formatBytes(bytes: number): string {
  if (!bytes) return '0 B'
  const units = ['B', 'kB', 'MB', 'GB', 'TB']
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1000)), units.length - 1)
  const value = bytes / 1000 ** power
  return `${value.toFixed(power === 0 ? 0 : 1)} ${units[power]}`
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return 'unknown'
  if (seconds < 90) return `${Math.round(seconds)} s`
  const minutes = seconds / 60
  if (minutes < 90) return `${Math.round(minutes)} min`
  const hours = minutes / 60
  return hours < 48 ? `${hours.toFixed(1)} h` : `${Math.round(hours / 24)} days`
}
