/**
 * Token parity (frontend styling-layer plan section 4.4).
 *
 * 1. Every COLOUR token declared on `:root` is either redeclared for dark, or resolves only
 *    through `var()` to tokens that are, or is listed in THEME_INVARIANT with a reason. A
 *    colour with no dark story renders its light value in dark mode (132 of 215 files with
 *    colour literals had exactly this problem before the token layer).
 * 2. Scale tokens (space, radius, font size, z-index, duration) are theme-independent and
 *    must NOT be redeclared in the dark block.
 * 3. Every `--shadow-*` token is declared in BOTH blocks (elevation reads differently on dark).
 * 4. Every `var(--x)` used in `src` resolves to a declared custom property. This replaces the
 *    safety the 600+ literal fallbacks only pretended to give: a fallback on an undefined
 *    token renders the literal in both themes and hides the typo.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { SRC, walkSource } from './sourceTree';
import { loadThemeTokens, resolveToken, type Theme, type TokenSets } from './themeTokens';

const sets = loadThemeTokens();

/** Colour tokens that are deliberately the same in both themes, each with its reason. */
const THEME_INVARIANT: Record<string, string> = {
  '--text-on-solid': 'white label on a solid fill: the fills are dark in both themes',
  '--text-on-warning': 'dark label on the amber fill, which is the same amber in both themes',
  '--media-letterbox':
    'the bars around a video picture are black in both themes, as the picture is',
  '--error-solid': 'solid danger fill, one value like --primary-color (white 4.83:1)',
  '--error-solid-hover': 'hover of --error-solid',
  '--warning-solid': 'amber fill carrying a dark label',
  '--warning-solid-hover': 'hover of --warning-solid',
  '--success-solid': 'solid success fill carrying a white label (5.48:1)',
  '--success-solid-hover': 'hover of --success-solid',
  '--dark-border': 'named for dark mode and used inside dark-scoped rules',
  '--dark-input-bg': 'named for dark mode and used inside dark-scoped rules',
  '--dark-text': 'named for dark mode and used inside dark-scoped rules',
  '--player-on-media':
    'player icons and text drawn over the video frame, which is the backdrop in both themes',
  '--player-scrim': 'gradient behind the controls over the video frame',
  '--player-scrim-solid': 'the frame colour the scrim is composited over (contrast rows)',
  '--player-control-hover': 'hover wash on a control over the video frame',
  '--player-track': 'time and volume track over the video frame',
  '--player-buffer': 'buffered range over the video frame',
  '--player-caption-bg': 'caption box over the video frame',
  '--player-caption-speaker': 'speaker name inside the caption box',
};

const COLOUR_VALUE = /#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(|^\d+\s*,\s*\d+\s*,\s*\d+$/;
const SCALE_PREFIX = /^--(space|radius|font-size|z|duration|ease|line-height|icon-size)-/;

/** Does this token have a dark story? Memoised; var() chains are followed through `root`. */
function isThemed(s: TokenSets, name: string, seen: string[] = []): boolean {
  if (s.dark.has(name)) return true;
  if (name in THEME_INVARIANT) return true;
  const raw = s.root.get(name);
  if (raw === undefined || seen.includes(name)) return false;
  const refs = [...raw.matchAll(/var\(\s*(--[\w-]+)/g)].map((m) => m[1]);
  if (refs.length === 0) return !COLOUR_VALUE.test(raw); // not a colour: nothing to theme
  return refs.every((r) => isThemed(s, r, [...seen, name]));
}

describe('token parity: colours', () => {
  const colourTokens = [...sets.root].filter(
    ([name, value]) =>
      !SCALE_PREFIX.test(name) &&
      !name.startsWith('--shadow-') &&
      !name.endsWith('shadow') &&
      (COLOUR_VALUE.test(value) || /var\(/.test(value))
  );

  it('finds a plausible number of colour tokens (the filter is not vacuous)', () => {
    expect(colourTokens.length).toBeGreaterThan(80);
  });

  it('every colour token on :root has a dark story or a written reason', () => {
    const missing = colourTokens.map(([name]) => name).filter((name) => !isThemed(sets, name));
    expect(missing).toEqual([]);
  });

  it('THEME_INVARIANT has no stale entries (each is declared and has no dark override)', () => {
    for (const name of Object.keys(THEME_INVARIANT)) {
      expect(sets.root.has(name), `${name} is not declared on :root`).toBe(true);
      // Redeclaring it in dark is allowed only if the value is identical (--error-solid is).
      if (sets.dark.has(name)) {
        expect(sets.dark.get(name), `${name} differs in dark: not invariant`).toBe(
          sets.root.get(name)
        );
      }
    }
  });
});

describe('token parity: scales are theme-independent', () => {
  it('no scale token is redeclared in the dark block', () => {
    const offenders = [...sets.dark.keys()].filter((n) => SCALE_PREFIX.test(n));
    expect(offenders).toEqual([]);
  });

  it('the scales exist on :root', () => {
    for (const n of [
      '--space-4',
      '--radius-lg',
      '--font-size-base',
      '--duration-base',
      '--z-modal',
    ]) {
      expect(sets.root.has(n), n).toBe(true);
    }
  });
});

describe('token parity: elevation', () => {
  it('every --shadow-* token is declared in both blocks', () => {
    const rootShadows = [...sets.root.keys()].filter((n) => n.startsWith('--shadow-'));
    expect(rootShadows.length).toBeGreaterThan(8);
    for (const n of rootShadows) expect(sets.dark.has(n), `${n} missing from dark`).toBe(true);
    for (const n of [...sets.dark.keys()].filter((k) => k.startsWith('--shadow-'))) {
      expect(sets.root.has(n), `${n} missing from :root`).toBe(true);
    }
  });
});

describe('token parity: one tint blue (decision D2)', () => {
  const channels = (theme: Theme, name: string) =>
    resolveToken(sets, theme, name)
      .split(',')
      .map((n) => Number(n.trim()));

  for (const theme of ['light', 'dark'] as Theme[]) {
    it(`${theme}: --accent-rgb is the --primary-color-rgb blue`, () => {
      expect(channels(theme, '--accent-rgb')).toEqual(channels(theme, '--primary-color-rgb'));
    });
  }

  it('control: the frozen tint blue it replaced is a different colour', () => {
    expect([59, 130, 246]).not.toEqual(channels('light', '--primary-color-rgb'));
  });
});

describe('token parity: no stylesheet restates an elevation token', () => {
  // A box-shadow written out with the value of a --shadow-* token, or with the
  // --primary-color-rgb spelling of --shadow-primary(-hover), misses the dark elevation the
  // token carries (decision D13) and drifts the next time the token moves.
  const norm = (v: string) => v.trim().replace(/\s+/g, ' ');
  const twins = new Map<string, string>();
  for (const [name, value] of sets.root) {
    if (!name.startsWith('--shadow-') || value === 'none') continue;
    twins.set(norm(value), name);
    twins.set(norm(value.replace(/--accent-rgb/g, '--primary-color-rgb')), name);
  }

  function restated(css: string): string[] {
    const text = css.replace(/\/\*[\s\S]*?\*\//g, '');
    return [...text.matchAll(/box-shadow\s*:\s*([^;{}]+);/g)]
      .map((m) => twins.get(norm(m[1])))
      .filter((name): name is string => name !== undefined);
  }

  it('every box-shadow equal to a token uses the token', () => {
    const offenders = walkSource(SRC, /\.(svelte|css)$/)
      .filter((f) => path.basename(f) !== 'theme.css')
      .flatMap((f) =>
        restated(fs.readFileSync(f, 'utf8')).map((t) => `${path.relative(SRC, f)}: ${t}`)
      );
    expect(offenders).toEqual([]);
  });

  it('control: a primary-blue twin of --shadow-primary-hover is caught', () => {
    expect(twins.size).toBeGreaterThan(10);
    expect(
      restated('.b:hover { box-shadow: 0 4px 8px rgba(var(--primary-color-rgb), 0.25); }')
    ).toEqual(['--shadow-primary-hover']);
  });
});

/** All custom properties declared anywhere under src (stylesheets, templates, scripts). */
describe('token parity: every var(--x) resolves', () => {
  const files = walkSource(SRC, /\.(svelte|css|ts|js|html)$/, {
    skipDirs: ['node_modules', '.svelte-kit'],
  });
  const declaredAnywhere = new Set<string>();
  const used: Array<{ name: string; file: string }> = [];
  for (const file of files) {
    const text = fs.readFileSync(file, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    for (const m of text.matchAll(/(?<![\w-])(--[a-zA-Z][\w-]*)\s*:/g)) declaredAnywhere.add(m[1]);
    for (const m of text.matchAll(/setProperty\(\s*['"`](--[\w-]+)/g)) declaredAnywhere.add(m[1]);
    for (const m of text.matchAll(/['"`](--[\w-]+)['"`]\s*:/g)) declaredAnywhere.add(m[1]);
    if (/\.test\.(ts|js)$/.test(file)) continue; // tests quote tokens as fixtures
    // Tokens built by interpolation (`var(--icon-size-${size})`) end in a dash: not checkable.
    for (const m of text.matchAll(/var\(\s*(--[\w-]*[\w])\s*[,)]/g)) {
      used.push({ name: m[1], file: path.relative(SRC, file) });
    }
  }

  it('scans a plausible amount of the tree', () => {
    expect(files.length).toBeGreaterThan(500);
    expect(used.length).toBeGreaterThan(3000);
  });

  it('no var() points at an undeclared custom property', () => {
    const undeclared = used
      .filter((u) => !declaredAnywhere.has(u.name))
      .map((u) => `${u.name}  (${u.file})`);
    expect([...new Set(undeclared)].sort()).toEqual([]);
  });

  it('control: a typo would be caught', () => {
    expect(declaredAnywhere.has('--surfce-color')).toBe(false);
    expect(declaredAnywhere.has('--surface-color')).toBe(true);
  });
});
