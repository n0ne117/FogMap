// SPDX-License-Identifier: AGPL-3.0-or-later
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))

// The version is read from the VERSION file rather than passed as a build
// argument, so there is no second place for it to drift out of sync.
// In the image VERSION sits next to this config; in a source checkout it is
// one directory up at the repository root.
function readVersion(): string {
  const candidates = [resolve(here, 'VERSION'), resolve(here, '..', 'VERSION')]
  for (const path of candidates) {
    try {
      const text = readFileSync(path, 'utf8').trim()
      if (text) return text
    } catch {
      // Try the next candidate.
    }
  }
  throw new Error(
    `Irfaran web cannot determine its version. No readable VERSION file at ${candidates.join(
      ' or ',
    )}. The build context did not include the repository root.`,
  )
}

export default defineConfig({
  define: {
    __IRFARAN_VERSION__: JSON.stringify(readVersion()),
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
