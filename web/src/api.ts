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

/** The token out of whatever was pasted.
 *
 * `irfaran.cli token` printed the token and then a line saying where it came
 * from, and the obvious thing to do with two lines of console output is copy
 * both - which was then rejected as the wrong token while the right one sat in
 * the clipboard one line up. A token has no whitespace in it, so the first
 * chunk of whatever arrives is the answer. That also absorbs a trailing
 * newline, a copied shell prompt, or a stray bullet.
 */
export function tokenFrom(pasted: string): string {
  const [first = ''] = clean(pasted).trim().split(/\s+/)
  return first
}

/** Invisible characters a copy out of a rendered web page can carry.
 *
 * JavaScript's \s covers the ordinary suspects - space, tab, newline,
 * non-breaking space, even a byte order mark - but not the zero-width family,
 * which survives trimming and splitting and then fails a byte comparison while
 * looking character for character correct on screen.
 */
const INVISIBLE = /[\u200b-\u200d\u2060\u180e\ufeff\u00ad]|[\u0000-\u001f\u007f]/g

export function clean(text: string): string {
  return text.replace(INVISIBLE, '')
}

/** What was actually sent, described without giving the secret away.
 *
 * "The server refused that token" is true and tells you nothing about which of
 * the several possible reasons it was. The length and the character set are
 * safe to show and usually name the problem on sight.
 */
export function describeToken(token: string): string {
  const odd = token.replace(/[0-9a-fA-F]/g, '')
  const parts = [`${token.length} characters`]
  if (odd) {
    parts.push(
      `including ${odd.length} that ${odd.length === 1 ? 'is' : 'are'} not a hex digit`,
    )
  }
  return parts.join(', ')
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

/**
 * A GET, optionally with a deadline.
 *
 * `timeoutMs` exists because nothing here had one. A status poll that never
 * answers left the caller awaiting it forever: the import screen sat at 100%
 * with the render long finished, because the loop watching it was still waiting
 * on a request that was never going to come back. Requests that are legitimately
 * slow - an upload, an export - pass no timeout and keep the old behaviour.
 */
export async function apiGet<T>(
  path: string,
  options: { timeoutMs?: number } = {},
): Promise<T> {
  const controller = options.timeoutMs ? new AbortController() : undefined
  const timer = controller
    ? window.setTimeout(() => controller.abort(), options.timeoutMs)
    : undefined

  try {
    const response = await fetch(path, {
      headers: { accept: 'application/json' },
      signal: controller?.signal,
    })
    return (await parse(response)) as T
  } finally {
    if (timer !== undefined) window.clearTimeout(timer)
  }
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
        'Settings, Security.',
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
