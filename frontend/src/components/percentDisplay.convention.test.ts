/**
 * A percentage printed beside Arabic text rendered as "%99": the percent sign is a weak
 * character, and in a right-to-left run it can detach from its number. Every markup site that
 * prints `{value}%` must therefore put the figure in its own left-to-right run (`dir="ltr"` on
 * the element, or a `<bdi dir="ltr">` around it).
 *
 * Static, like the formatter convention: it reads every component, so a percent added anywhere
 * fails here without a render test having to mount it.
 */
import { describe, it, expect } from 'vitest';

const sources = import.meta.glob(['/src/**/*.svelte', '!/src/**/*.test.ts'], {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const blank = (m: string) => m.replace(/[^\n]/g, ' ');

/** Markup only: scripts, styles and comments blanked so line numbers survive. */
export function markupOf(src: string): string {
  return src
    .replace(/<script[\s\S]*?<\/script>/g, blank)
    .replace(/<style[\s\S]*?<\/style>/g, blank)
    .replace(/<!--[\s\S]*?-->/g, blank);
}

/** `<line>` of each `{expression}%` that is not inside an element or `<bdi>` marked `dir="ltr"`. */
export function unisolatedPercents(src: string): number[] {
  const out: number[] = [];
  markupOf(src)
    .split('\n')
    .forEach((line, i) => {
      // `${n}%` inside an attribute or a callback is a string, not a text node.
      const text = line.replace(/\$\{[^{}]*\}\s?%/g, '');
      if (/\}\s?%(?![;"')\w])/.test(text) && !/dir="ltr"/.test(line)) out.push(i + 1);
    });
  return out;
}

describe('percentages in markup are isolated left-to-right', () => {
  it('flags a bare percent and accepts an isolated one', () => {
    expect(unisolatedPercents('<span>{n}%</span>')).toEqual([1]);
    expect(unisolatedPercents('<span dir="ltr">{n}%</span>')).toEqual([]);
    expect(unisolatedPercents('<p>a <bdi dir="ltr">{n}%</bdi></p>')).toEqual([]);
    expect(unisolatedPercents('<script>const s = `${n}%`;</script>')).toEqual([]);
  });

  it('scans a real tree', () => {
    expect(Object.keys(sources).length).toBeGreaterThan(100);
  });

  it('has no unisolated {value}% in any component', () => {
    const offenders = Object.entries(sources).flatMap(([path, src]) =>
      unisolatedPercents(src).map((line) => `${path}:${line}`)
    );
    expect(offenders).toEqual([]);
  });
});
