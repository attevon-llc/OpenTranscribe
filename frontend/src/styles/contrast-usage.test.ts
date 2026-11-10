/**
 * WCAG 2.2 AA contrast for the token pairs the stylesheets ACTUALLY put together.
 *
 * `contrast-pairs.test.ts` checks a declared table of pairs. A table only covers what someone
 * remembered to write down: on 2026-11-10 it was green while 81 rules coloured text with the
 * status fill `--error-color` (3.76:1 light) and `--text-tertiary` sat at 2.56:1. This test
 * derives the pairs from the source instead, so a new pairing is checked the day it is written:
 *
 * 1. Every rule body in a component `<style>` block or a `src/styles/*.css` file that sets
 *    `color: var(--fg)` AND `background[-color]: var(--bg)` (or `rgba(var(--x-rgb), a)`) is a
 *    text-on-background pair. A translucent background is composited over `--surface-color`.
 * 2. A text-role token set with no background in its rule inherits a surface, so it is checked
 *    on both base surfaces (`--surface-color`, `--background-color`).
 *
 * Text needs 4.5:1 (SC 1.4.3, https://www.w3.org/TR/WCAG22/#contrast-minimum); the ratio is the
 * relative-luminance formula of https://www.w3.org/TR/WCAG22/#dfn-contrast-ratio, implemented
 * once in `themeTokens.ts`. Large text could take 3:1, but a token does not know its size, so
 * every pair is held to 4.5. Tokens declared outside the global stylesheets (a component's own
 * custom property) cannot be resolved statically and are skipped; the count of resolved pairs is
 * asserted so the scan cannot quietly go empty.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  compositeOver,
  contrastRatio,
  parseColour,
  parseTokenSets,
  resolveToken,
  type Colour,
  type Rgb,
  type Theme,
  type TokenSets,
} from './themeTokens';

const SRC = path.resolve(__dirname, '..');
const STYLES = path.join(SRC, 'styles');
const TEXT = 4.5;
const THEMES: Theme[] = ['light', 'dark'];

/** Token sets from theme.css plus every other global stylesheet's `:root` / dark blocks. */
function loadAllTokens(): TokenSets {
  const sets: TokenSets = { root: new Map(), dark: new Map() };
  const files = fs.readdirSync(STYLES).filter((f) => f.endsWith('.css'));
  // theme.css last, so a role token is never shadowed by a component stylesheet.
  files.sort((a, b) => Number(a === 'theme.css') - Number(b === 'theme.css'));
  for (const f of files) {
    const parsed = parseTokenSets(fs.readFileSync(path.join(STYLES, f), 'utf8'));
    for (const [k, v] of parsed.root) sets.root.set(k, v);
    for (const [k, v] of parsed.dark) sets.dark.set(k, v);
  }
  return sets;
}

const sets = loadAllTokens();

function walk(dir: string, out: string[] = []): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) walk(p, out);
    else if (/\.(svelte|css)$/.test(e.name)) out.push(p);
  }
  return out;
}

const rel = (p: string) => path.relative(SRC, p).split(path.sep).join('/');

/** Rule bodies (innermost `{...}` with no nested braces) of a file's stylesheet text. */
function ruleBodies(file: string): string[] {
  let text = fs.readFileSync(file, 'utf8');
  if (file.endsWith('.svelte')) {
    text = [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
  }
  text = text.replace(/\/\*[\s\S]*?\*\//g, '');
  return [...text.matchAll(/\{([^{}]*)\}/g)].map((m) => m[1]);
}

/** A background: a token name, or `rgba(var(--x-rgb), a)` as `{ rgb, alpha }`. */
type Bg = { token: string } | { rgb: string; alpha: number };

const FG_RE = /(?<![-\w])color\s*:\s*var\((--[\w-]+)\)\s*(?:;|$)/g;
const BG_RE =
  /(?<![-\w])background(?:-color)?\s*:\s*(?:var\((--[\w-]+)\)|rgba\(\s*var\((--[\w-]+)\)\s*,\s*([\d.]+)\s*\))\s*(?:;|$)/g;

/** Tokens that colour text (by role), checked on the base surfaces when a rule sets no bg. */
const TEXT_ROLE =
  /^--(text-(?!on-)[\w-]+|color-text[\w-]*|[\w-]+-text|primary-on-surface|link-[\w-]+)$/;

interface Use {
  fg: string;
  bg: Bg | null;
  files: Set<string>;
}

/** The `[fg, bg]` pairs one rule body sets; a text-role fg with no background gets `null`. */
function pairsInRule(body: string): [string, Bg | null][] {
  const fgs = [...body.matchAll(FG_RE)].map((m) => m[1]);
  const bgs: Bg[] = [...body.matchAll(BG_RE)].map((m) =>
    m[1] ? { token: m[1] } : { rgb: m[2], alpha: Number(m[3]) }
  );
  if (bgs.length) return fgs.flatMap((fg) => bgs.map((bg): [string, Bg] => [fg, bg]));
  return fgs.filter((fg) => TEXT_ROLE.test(fg)).map((fg): [string, null] => [fg, null]);
}

const useKey = (fg: string, bg: Bg | null) =>
  `${fg} on ${
    bg === null ? '(inherited surface)' : 'token' in bg ? bg.token : `${bg.rgb}@${bg.alpha}`
  }`;

function collectUses(): Map<string, Use> {
  const uses = new Map<string, Use>();
  for (const file of walk(SRC)) {
    for (const [fg, bg] of ruleBodies(file).flatMap(pairsInRule)) {
      const key = useKey(fg, bg);
      const u = uses.get(key) ?? { fg, bg, files: new Set<string>() };
      u.files.add(rel(file));
      uses.set(key, u);
    }
  }
  return uses;
}

function tryColour(theme: Theme, token: string): Colour | null {
  try {
    return parseColour(resolveToken(sets, theme, token));
  } catch {
    return null;
  }
}

function opaque(theme: Theme, token: string): Rgb {
  const c = tryColour(theme, token);
  if (!c || c.alpha !== 1) throw new Error(`${token} is not an opaque colour in ${theme}`);
  return c.rgb;
}

/** Composited background colours a use is checked against; null when it cannot be resolved. */
function backgrounds(theme: Theme, bg: Bg | null): Rgb[] | null {
  const surface = opaque(theme, '--surface-color');
  if (bg === null) return [surface, opaque(theme, '--background-color')];
  if ('token' in bg) {
    const c = tryColour(theme, bg.token);
    return c ? [compositeOver(c, surface)] : null;
  }
  let channels: string;
  try {
    channels = resolveToken(sets, theme, bg.rgb);
  } catch {
    return null;
  }
  return [compositeOver(parseColour(`rgba(${channels}, ${bg.alpha})`), surface)];
}

/** Pairs that are not text on that background, each with the reason. Key as in `collectUses`. */
const NOT_TEXT_ON_BG: Record<string, string> = {};

interface Result {
  key: string;
  theme: Theme;
  ratio: number;
  files: string[];
}

function evaluate(): { resolved: number; failures: Result[]; stale: string[] } {
  const uses = collectUses();
  const failures: Result[] = [];
  let resolved = 0;
  for (const [key, use] of uses) {
    if (key in NOT_TEXT_ON_BG) continue;
    for (const theme of THEMES) {
      const fg = tryColour(theme, use.fg);
      const bgs = backgrounds(theme, use.bg);
      if (!fg || !bgs) continue;
      resolved++;
      for (const bg of bgs) {
        const ratio = contrastRatio(compositeOver(fg, bg), bg);
        if (ratio < TEXT) {
          failures.push({
            key,
            theme,
            ratio: Math.round(ratio * 100) / 100,
            files: [...use.files],
          });
        }
      }
    }
  }
  const stale = Object.keys(NOT_TEXT_ON_BG).filter((k) => !uses.has(k));
  return { resolved, failures, stale };
}

describe('token pairs used together in the stylesheets clear WCAG 2.2 AA', () => {
  const { resolved, failures, stale } = evaluate();

  it('every text/background pair found in a rule, and every inherited text token, is >= 4.5:1', () => {
    const report = failures.map(
      (f) => `${f.theme}: ${f.key} = ${f.ratio}:1 (${f.files.slice(0, 3).join(', ')})`
    );
    expect(report).toEqual([]);
  });

  it('resolves enough pairs that an empty scan cannot pass', () => {
    expect(resolved).toBeGreaterThanOrEqual(200);
  });

  it('has no stale NOT_TEXT_ON_BG entries', () => {
    expect(stale).toEqual([]);
  });

  it('control: a status fill used as text on the light card fails the bar', () => {
    // #ef4444 on white is 3.76:1: the detector must call this a failure.
    expect(contrastRatio([0xef, 0x44, 0x44], [255, 255, 255])).toBeLessThan(TEXT);
  });
});
