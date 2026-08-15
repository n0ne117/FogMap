// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Carrying settings across the rename.
//
// Everything the browser remembers was stored under a `fogmap.` prefix until
// 0.10.0 - including the API token, without which the map is read-only. An
// upgrade that silently forgets it looks exactly like being locked out, so
// anything still under the old prefix is copied to the new one the first time
// the page loads, and the old copy is left alone in case of a rollback.

const OLD = 'fogmap.'
const NEW = 'irfaran.'

export function carryOldSettings(): number {
  let carried = 0
  try {
    for (const key of Object.keys(window.localStorage)) {
      if (!key.startsWith(OLD)) continue

      const renamed = NEW + key.slice(OLD.length)
      if (window.localStorage.getItem(renamed) !== null) continue

      const value = window.localStorage.getItem(key)
      if (value === null) continue

      window.localStorage.setItem(renamed, value)
      carried += 1
    }
  } catch {
    /* a browser that will not let us read storage has nothing to carry */
  }
  return carried
}
