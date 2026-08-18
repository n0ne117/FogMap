// SPDX-License-Identifier: AGPL-3.0-or-later
//
// Monochrome line icons, drawn here rather than pulled from a library.
//
// An icon set is a dependency, a licence and a few hundred kilobytes for the
// half dozen glyphs this interface needs, and the emoji it replaces brought
// their own problem: they render in whatever colour and style the operating
// system feels like, which is why a folder's eye was a different shape on every
// machine and could not be made to match the text beside it.
//
// These are plain geometry on a 24 unit grid, stroked in currentColor, so they
// take the colour and the weight of whatever they sit in and follow the theme
// without being told about it.

const SVG = 'http://www.w3.org/2000/svg'

type Shape =
  | { circle: [number, number, number] }
  | { path: string }
  | { line: [number, number, number, number] }

const SHAPES: Record<string, Shape[]> = {
  // An eye: a lens and a pupil. Visible.
  eye: [
    { path: 'M1.5 12s4-6.5 10.5-6.5S22.5 12 22.5 12s-4 6.5-10.5 6.5S1.5 12 1.5 12z' },
    { circle: [12, 12, 3] },
  ],

  // The same eye with a stroke through it. Hidden.
  'eye-off': [
    { path: 'M1.5 12s4-6.5 10.5-6.5S22.5 12 22.5 12s-4 6.5-10.5 6.5S1.5 12 1.5 12z' },
    { circle: [12, 12, 3] },
    { line: [3, 21, 21, 3] },
  ],

  // Four arrows from a centre: the conventional sign for panning, and clearer
  // than a hand, which reads as "stop" as often as it reads as "grab".
  move: [
    { line: [12, 4, 12, 20] },
    { line: [4, 12, 20, 12] },
    { path: 'M12 2.5 9.5 6h5L12 2.5z' },
    { path: 'M12 21.5 9.5 18h5L12 21.5z' },
    { path: 'M2.5 12 6 9.5v5L2.5 12z' },
    { path: 'M21.5 12 18 9.5v5l3.5 2.5z' },
  ],

  // A cog: hub, ring, and eight teeth on the diagonals and axes. Generated
  // geometry rather than eyeballed coordinates, so the teeth are even.
  gear: [
    { circle: [12, 12, 3.4] },
    { circle: [12, 12, 6.5] },
    { line: [18.50, 12.00, 21.00, 12.00] },
    { line: [16.60, 16.60, 18.36, 18.36] },
    { line: [12.00, 18.50, 12.00, 21.00] },
    { line: [7.40, 16.60, 5.64, 18.36] },
    { line: [5.50, 12.00, 3.00, 12.00] },
    { line: [7.40, 7.40, 5.64, 5.64] },
    { line: [12.00, 5.50, 12.00, 3.00] },
    { line: [16.60, 7.40, 18.36, 5.64] },
  ],
  plus: [
    { line: [12, 5, 12, 19] },
    { line: [5, 12, 19, 12] },
  ],

  trash: [
    { line: [4, 7, 20, 7] },
    { path: 'M6 7v12.5A1.5 1.5 0 0 0 7.5 21h9a1.5 1.5 0 0 0 1.5-1.5V7' },
    { path: 'M9.5 7V4.5A1.5 1.5 0 0 1 11 3h2a1.5 1.5 0 0 1 1.5 1.5V7' },
  ],

  pencil: [
    { path: 'M4 20h4L20 8l-4-4L4 16v4z' },
    { line: [14.5, 5.5, 18.5, 9.5] },
  ],

  close: [
    { line: [5, 5, 19, 19] },
    { line: [19, 5, 5, 19] },
  ],
}

/**
 * One icon, as an inline SVG element.
 *
 * `aria-hidden`, because every caller is a button that already has a label:
 * a screen reader reading "eye" adds nothing to "Hide folder".
 */
export function icon(name: keyof typeof SHAPES | string, size = 16): SVGSVGElement {
  const shapes = SHAPES[name]
  if (!shapes) {
    throw new Error(`No icon called ${name}. Add it to web/src/icons.ts.`)
  }

  const svg = document.createElementNS(SVG, 'svg')
  svg.setAttribute('viewBox', '0 0 24 24')
  svg.setAttribute('width', String(size))
  svg.setAttribute('height', String(size))
  svg.setAttribute('fill', 'none')
  svg.setAttribute('stroke', 'currentColor')
  svg.setAttribute('stroke-width', '1.8')
  svg.setAttribute('stroke-linecap', 'round')
  svg.setAttribute('stroke-linejoin', 'round')
  svg.setAttribute('aria-hidden', 'true')
  svg.classList.add('icon')

  for (const shape of shapes) {
    if ('circle' in shape) {
      const [cx, cy, r] = shape.circle
      const node = document.createElementNS(SVG, 'circle')
      node.setAttribute('cx', String(cx))
      node.setAttribute('cy', String(cy))
      node.setAttribute('r', String(r))
      svg.append(node)
    } else if ('line' in shape) {
      const [x1, y1, x2, y2] = shape.line
      const node = document.createElementNS(SVG, 'line')
      node.setAttribute('x1', String(x1))
      node.setAttribute('y1', String(y1))
      node.setAttribute('x2', String(x2))
      node.setAttribute('y2', String(y2))
      svg.append(node)
    } else {
      const node = document.createElementNS(SVG, 'path')
      node.setAttribute('d', shape.path)
      svg.append(node)
    }
  }

  return svg
}

/** Replace a button's contents with an icon, keeping its accessible name. */
export function setIcon(button: HTMLElement, name: string, size = 16): void {
  button.textContent = ''
  button.append(icon(name, size))
}

export const ICON_NAMES = Object.keys(SHAPES)

/**
 * Fill every `[data-icon]` placeholder in the markup.
 *
 * Lets static chrome ask for an icon without every one of them needing a
 * handle in TypeScript - the Pan button says `data-icon="move"` and that is
 * the whole wiring.
 */
export function hydrateIcons(root: ParentNode = document): void {
  for (const host of root.querySelectorAll<HTMLElement>('[data-icon]')) {
    const name = host.dataset.icon
    if (!name || host.firstElementChild) continue
    host.append(icon(name, Number(host.dataset.iconSize ?? 14)))
  }
}
