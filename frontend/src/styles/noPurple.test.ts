/**
 * The app has no purple (issue #1244 item 6b).
 *
 * Two real defects shipped this way: `a:visited { color: var(--link-visited) }` made every visited
 * link indigo (the Settings "Open full screen" link read purple), and a handful of one-off action
 * buttons and badges used Tailwind indigo/violet next to the app's blue. A style source scan is
 * the only observer: jsdom applies no `:visited` styling and computes no cascade.
 *
 * Fails on, in `.svelte` / `.css` / `.ts` source (comments stripped):
 * - any hex / rgb() / rgba() / hsl() / hsla() colour whose hue is violet-to-magenta (232..335
 *   degrees, with enough saturation to read as a colour, not a grey);
 * - the colour keywords purple, violet, indigo, magenta, fuchsia, orchid, plum, lavender;
 * - a `:visited` rule that sets a colour (visited links keep the link blue: the global
 *   `a { color }` already applies to them, and a `:visited` rule out-specifies every
 *   `.btn`-style anchor and turns its label a different colour).
 *
 * Every allowlist entry carries a written reason and is itself checked for staleness.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '..');

function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(svelte|css|ts)$/.test(e.name) && !/\.test\.ts$/.test(e.name)) out.push(p);
  }
  return out;
}

const rel = (p: string) => path.relative(SRC, p).split(path.sep).join('/');
const stripComments = (s: string) =>
  s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/<!--[\s\S]*?-->/g, '')
    .replace(/(^|[^:'"`])\/\/[^\n]*/g, '$1');

const FILES = walk(SRC);

/** Hue (degrees) and saturation (0..1) of an sRGB colour; lightness is returned for the near-black/white cut. */
export function hsl(r: number, g: number, b: number): { h: number; s: number; l: number } {
  const [rn, gn, bn] = [r / 255, g / 255, b / 255];
  const max = Math.max(rn, gn, bn);
  const min = Math.min(rn, gn, bn);
  const l = (max + min) / 2;
  const d = max - min;
  if (d === 0) return { h: 0, s: 0, l };
  const s = d / (1 - Math.abs(2 * l - 1));
  let h: number;
  if (max === rn) h = ((gn - bn) / d) % 6;
  else if (max === gn) h = (bn - rn) / d + 2;
  else h = (rn - gn) / d + 4;
  return { h: (h * 60 + 360) % 360, s, l };
}

export function isPurple({ h, s, l }: { h: number; s: number; l: number }): boolean {
  // Greys, near-black navy and near-white tints are not "a purple": cut on saturation/lightness.
  if (s < 0.2 || l < 0.16 || l > 0.97) return false;
  return h >= 232 && h < 335;
}

const HEX = /(?<![&\w])#([0-9a-f]{8}|[0-9a-f]{6}|[0-9a-f]{3,4})(?![0-9a-z_-])/gi;
const RGB = /rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})/gi;
const HSL = /hsla?\(\s*(-?[\d.]+)(?:deg)?\s*[, ]\s*([\d.]+)%\s*[, ]\s*([\d.]+)%/gi;
const KEYWORD =
  /(?<![-\w#.$'"])(purple|violet|indigo|magenta|fuchsia|orchid|plum|lavender)(?![-\w])/gi;
const VISITED_COLOR = /:visited[^{}]*\{[^}]*(?<![-\w])color\s*:/gi;

/** The purple colour literals in one source text. */
export function purpleLiterals(text: string): string[] {
  const src = stripComments(text);
  const found: string[] = [];
  for (const m of src.matchAll(HEX)) {
    let hex = m[1];
    if (hex.length <= 4) hex = [...hex].map((c) => c + c).join('');
    const [r, g, b] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
    if (isPurple(hsl(r, g, b))) found.push(m[0]);
  }
  for (const m of src.matchAll(RGB)) {
    if (isPurple(hsl(+m[1], +m[2], +m[3]))) found.push(m[0] + ')');
  }
  for (const m of src.matchAll(HSL)) {
    const h = ((+m[1] % 360) + 360) % 360;
    // hsl() lightness is already the L we cut on; reuse the same thresholds.
    if (isPurple({ h, s: +m[2] / 100, l: +m[3] / 100 })) found.push(m[0] + ')');
  }
  for (const m of src.matchAll(KEYWORD)) found.push(m[0]);
  for (const m of src.matchAll(VISITED_COLOR)) found.push(m[0].split('{')[0].trim());
  return found;
}

/**
 * Files allowed to carry a purple, each with the reason. These are DATA, not chrome: removing the
 * colour would remove information, not decoration.
 */
const ALLOW: Record<string, string> = {
  'lib/utils/speakerColors.ts':
    'speaker identity palette (data visualisation): twenty-odd distinguishable swatches, a violet among them keeps neighbouring speakers apart. Flagged for the owner, not changed here.',
  'components/LoginBanner.svelte':
    'CUI classification marking colour (NARA/ISOO standard marking), a regulatory colour, not app chrome',
  'lib/components/ClassificationBanner.svelte':
    'CUI classification banner colour (NARA/ISOO standard marking), a regulatory colour, not app chrome',
};

describe('the app has no purple', () => {
  const hits = new Map<string, string[]>();
  for (const f of FILES) {
    const found = purpleLiterals(fs.readFileSync(f, 'utf8'));
    if (found.length) hits.set(rel(f), found);
  }

  it('uses the app blue (or a neutral) instead of a violet/indigo/purple literal', () => {
    const offenders = [...hits]
      .filter(([f]) => !(f in ALLOW))
      .map(([f, found]) => `${f}: ${[...new Set(found)].join(', ')}`);
    expect(offenders).toEqual([]);
  });

  it('has no stale allowlist entries', () => {
    expect(Object.keys(ALLOW).filter((f) => !hits.has(f))).toEqual([]);
  });

  it('carries a written reason for every allowlist entry', () => {
    for (const reason of Object.values(ALLOW)) expect(reason.length).toBeGreaterThan(30);
  });
});

describe('the detector can fire (a guard that cannot fail is not a guard)', () => {
  it.each([
    ['#4f46e5', 'indigo hex'],
    ['#6366f1', 'indigo-500 hex'],
    ['#818cf8', 'indigo-400 hex'],
    ['#a855f7', 'purple hex'],
    ['#8b5cf6', 'violet hex'],
    ['#7c3aed', 'violet-600 hex'],
    ['#a5f', 'short hex'],
    ['rgba(99, 102, 241, 0.12)', 'indigo rgba'],
    ['rgb(168 85 247)', 'space-separated rgb'],
    ['hsl(270, 60%, 50%)', 'purple hsl'],
    ['color: purple', 'keyword'],
    ['a:visited { color: var(--x); }', ':visited colour'],
  ])('flags %s (%s)', (literal) => {
    expect(purpleLiterals(`.x { prop: ${literal}; }`).length).toBeGreaterThan(0);
  });

  it.each([
    ['#2563eb', 'link blue'],
    ['#1d4ed8', 'link hover blue'],
    ['#60a5fa', 'dark link blue'],
    ['#3b82f6', 'primary blue'],
    ['#1e3a8a', 'navy'],
    ['#6b7280', 'grey'],
    ['#475569', 'slate'],
    ['#0ea5e9', 'sky'],
    ['rgba(59, 130, 246, 0.4)', 'blue rgba'],
    ['hsl(220, 80%, 50%)', 'blue hsl'],
  ])('leaves %s alone (%s)', (literal) => {
    expect(purpleLiterals(`.x { prop: ${literal}; }`)).toEqual([]);
  });

  it('ignores purple that only appears in a comment', () => {
    expect(purpleLiterals('/* was purple #6366f1 */ .x { color: #2563eb; } // indigo')).toEqual([]);
  });

  it('allows a :visited rule that sets no colour', () => {
    expect(purpleLiterals('a:visited { text-decoration: none; }')).toEqual([]);
  });
});
