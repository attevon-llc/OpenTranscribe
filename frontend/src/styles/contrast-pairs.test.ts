/**
 * Static WCAG 2.2 AA contrast for every documented token pair, in both themes.
 *
 * `docs/design/frontend_styling_layer_plan.md` section 3.10. The pairs are DATA, one row per
 * pair, so a new token earns a row instead of a hand-written test. Alpha tints are composited
 * over the named surface first: a status badge paints a translucent wash UNDER its text, and
 * the ratio against the plain surface does not transfer (issue #972: 5.29:1 plain, 4.38:1
 * composited, same token).
 *
 * The resolver is `themeTokens.ts`; `axe` still scans the rendered surfaces, this catches a
 * token edit that quietly drops a pair under the bar without needing a browser.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import {
  compositeOver,
  contrastRatio,
  loadThemeTokens,
  tokenColour,
  type Rgb,
  type Theme,
} from './themeTokens';

const sets = loadThemeTokens();
const BOTH: Theme[] = ['light', 'dark'];

/** A background: a token, or a translucent token composited over another token's colour. */
type Background = string | { tint: string; over: string };

interface Pair {
  fg: string;
  bg: Background;
  /** 4.5 for text (SC 1.4.3), 3 for non-text (SC 1.4.11). */
  min: number;
  themes: Theme[];
  reason: string;
}

const TEXT = 4.5;
const NON_TEXT = 3;

function surfaceRgb(theme: Theme, name: string): Rgb {
  const c = tokenColour(sets, theme, name);
  if (c.alpha !== 1) throw new Error(`${name} is translucent; name a surface to composite over`);
  return c.rgb;
}

function backgroundRgb(theme: Theme, bg: Background): Rgb {
  if (typeof bg === 'string') return surfaceRgb(theme, bg);
  return compositeOver(tokenColour(sets, theme, bg.tint), surfaceRgb(theme, bg.over));
}

function fgRgb(theme: Theme, name: string, onto: Rgb): Rgb {
  // A translucent foreground (none today) is composited over its own background.
  return compositeOver(tokenColour(sets, theme, name), onto);
}

const SURFACES = ['--surface-color', '--background-color', '--hover-color', '--surface-secondary'];
const STATUS = ['error', 'warning', 'success', 'info'];
const SYNTAX = ['keyword', 'string', 'number', 'title', 'comment', 'meta', 'deletion', 'addition'];

const PAIRS: Pair[] = [
  // text on surfaces
  ...['--text-color', '--primary-on-surface'].flatMap((fg) =>
    SURFACES.map((bg) => ({
      fg,
      bg,
      min: TEXT,
      themes: BOTH,
      reason: 'body / blue text on a surface',
    }))
  ),
  // Secondary and muted text inherit whatever surface they sit on, hover rows included
  // (contrast-usage.test.ts found --text-secondary at 4.34:1 on --hover-color and
  // --text-tertiary at 2.56:1 on the card), so they are held on every surface.
  ...['--text-secondary', '--text-muted'].flatMap((fg) =>
    SURFACES.map((bg) => ({
      fg,
      bg,
      min: TEXT,
      themes: BOTH,
      reason: 'secondary / muted text on a surface',
    }))
  ),
  // Tertiary text sits on sunken panels too (settings hints on --surface-secondary were
  // 4.34:1 with #64748b), so it is held on every surface.
  ...SURFACES.map((bg) => ({
    fg: '--text-tertiary',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'tertiary text on every surface',
  })),
  ...['--link-color', '--link-hover'].flatMap((fg) =>
    ['--surface-color', '--background-color'].map((bg) => ({
      fg,
      bg,
      min: TEXT,
      themes: BOTH,
      reason: 'link text, resting and hovered',
    }))
  ),
  // The stronger secondary tone for a hover or sunken surface (--text-on-tint).
  ...['--hover-color', '--surface-secondary'].map((bg) => ({
    fg: '--text-on-tint',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'secondary text on a hover or sunken surface',
  })),
  // status text on its own tint, composited over the card surface
  ...STATUS.map((family) => ({
    fg: `--${family}-text`,
    bg: { tint: `--${family}-bg`, over: '--surface-color' },
    min: TEXT,
    themes: BOTH,
    reason: `${family} text on its own tint`,
  })),
  ...STATUS.map((family) => ({
    fg: '--text-on-tint',
    bg: { tint: `--${family}-bg`, over: '--surface-color' },
    min: TEXT,
    themes: BOTH,
    reason: `secondary text on the ${family} tint`,
  })),
  // labels on solid fills
  ...[
    '--primary-color',
    '--primary-hover',
    '--error-solid',
    '--error-solid-hover',
    '--success-solid',
    '--success-solid-hover',
    '--info-solid',
    '--info-solid-hover',
  ].map((bg) => ({
    fg: '--text-on-solid',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'white label on a solid fill',
  })),
  // The header buttons of the upload tray darken the primary bar rather than lighten it: a white
  // wash would leave the label at about 3.7:1 (3.0:1 hovered).
  ...['--on-solid-wash', '--on-solid-wash-hover'].map((tint) => ({
    fg: '--text-on-solid',
    bg: { tint, over: '--primary-color' },
    min: TEXT,
    themes: BOTH,
    reason: 'white label on a button wash over the primary bar',
  })),
  ...['--warning-solid', '--warning-solid-hover'].map((bg) => ({
    fg: '--text-on-warning',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'dark label on the amber fill (white is 2.15:1)',
  })),
  // media player chrome over the picture (issue #1327): composited over black, the darkest
  // frame. A bright frame lowers these, which is why the scrim and the caption box exist.
  ...['--player-scrim', '--player-caption-bg'].map((tint) => ({
    fg: '--player-on-media',
    bg: { tint, over: '--player-scrim-solid' },
    min: TEXT,
    themes: BOTH,
    reason: 'player label or caption text over the video',
  })),
  {
    fg: '--player-caption-speaker',
    bg: { tint: '--player-caption-bg', over: '--player-scrim-solid' },
    min: TEXT,
    themes: BOTH,
    reason: 'the caption speaker name inside the caption box',
  },
  // A search-hit mark keeps the text colour on top of it (styles/search.css, the default <mark>).
  {
    fg: '--text-primary',
    bg: { tint: '--highlight-bg', over: '--surface-color' },
    min: TEXT,
    themes: BOTH,
    reason: 'text on a search-hit mark',
  },
  // Hover tooltip chip (LLMConfigModal, PromptSettings).
  {
    fg: '--tooltip-text',
    bg: '--tooltip-bg',
    min: TEXT,
    themes: BOTH,
    reason: 'tooltip text on its chip',
  },
  // Code syntax colours on the code block, whose background is --surface-color (ChatMarkdown).
  ...SYNTAX.map((role) => ({
    fg: `--syntax-${role}`,
    bg: '--surface-color',
    min: TEXT,
    themes: BOTH,
    reason: 'code syntax colour on the code block',
  })),
  // non-text
  // The avatar overlay's camera icon over a picture. The scrim is one value in both themes and
  // the light card is white, the brightest picture it can sit on, so that row is the worst case.
  {
    fg: '--text-on-solid',
    bg: { tint: '--media-scrim', over: '--surface-color' },
    min: NON_TEXT,
    themes: ['light'],
    reason: 'white icon on the media scrim over a white picture (SC 1.4.11)',
  },
  ...['--focus-ring-color', '--control-border'].flatMap((fg) =>
    ['--surface-color', '--background-color'].map((bg) => ({
      fg,
      bg,
      min: NON_TEXT,
      themes: BOTH,
      reason: 'focus ring / control boundary against its surface (SC 1.4.11)',
    }))
  ),
];

describe('token contrast pairs (WCAG 2.2 AA)', () => {
  for (const pair of PAIRS) {
    for (const theme of pair.themes) {
      const bgName = typeof pair.bg === 'string' ? pair.bg : `${pair.bg.tint} over ${pair.bg.over}`;
      it(`${theme}: ${pair.fg} on ${bgName} >= ${pair.min}:1 (${pair.reason})`, () => {
        const bg = backgroundRgb(theme, pair.bg);
        const ratio = contrastRatio(fgRgb(theme, pair.fg, bg), bg);
        expect(ratio).toBeGreaterThanOrEqual(pair.min);
      });
    }
  }

  it('has a row for every status family and for both threshold classes', () => {
    // Guards the table itself: an emptied or truncated PAIRS list would make every check
    // above vacuously green.
    expect(PAIRS.length).toBeGreaterThanOrEqual(30);
    expect(PAIRS.some((p) => p.min === NON_TEXT)).toBe(true);
    for (const family of STATUS) {
      expect(PAIRS.some((p) => p.fg === `--${family}-text`)).toBe(true);
    }
  });

  it('control: the values this plan replaces would fail their pair', () => {
    // Proves the checker discriminates. White on #ef4444 (the old solid danger) is 3.76:1,
    // white on amber is 2.15:1, #d1d5db (the old field border) on white is 1.47:1.
    const white: Rgb = [255, 255, 255];
    expect(contrastRatio([255, 255, 255], [0xef, 0x44, 0x44])).toBeLessThan(TEXT);
    expect(contrastRatio(white, [0xf5, 0x9e, 0x0b])).toBeLessThan(TEXT);
    expect(contrastRatio([0xd1, 0xd5, 0xdb], white)).toBeLessThan(NON_TEXT);
  });
});

/**
 * Find marks and rings (`styles/search.css`, SegmentSpeakerDropdown's chip ring). Two roles,
 * checked separately, read from the stylesheets themselves so a retuned rule is checked the day
 * it is written:
 *
 * - FILL: a `background` mixed down from `--highlight-search` / `--highlight-current`
 *   (`color-mix(in srgb, var(--token) N%, transparent)`). Text sits on it: `--text-primary`
 *   needs 4.5:1 (SC 1.4.3).
 * - RING: a `border`, `box-shadow` or `outline` drawn in `--highlight-search-ring` /
 *   `--highlight-current-ring`. It is the only sign of a hit on a speaker chip and of the
 *   current hit, so it needs 3:1 against the surface (SC 1.4.11).
 * A fill token may not draw a ring and a ring token may not fill a mark. A rule with no
 * `[data-theme='dark']` prefix applies in light, and in dark unless a dark rule for the same
 * selector sets the same role.
 */
describe('find marks and rings (WCAG 2.2 AA)', () => {
  const MARK_SURFACES = ['--surface-color', '--background-color'];
  const DARK_PREFIXES = ["[data-theme='dark'] ", ":global([data-theme='dark']) "];
  const HIGHLIGHT = /var\((--highlight-(?:search|current)(?:-ring)?)\)/;
  const PERCENT = /\)\s+([\d.]+)%\s*,\s*transparent/;
  const SOURCES = ['styles/search.css', 'components/SegmentSpeakerDropdown.svelte'];

  interface MarkUse {
    file: string;
    selector: string;
    role: 'fill' | 'ring';
    token: string;
    percent: number;
    dark: boolean;
  }

  function styleText(file: string): string {
    let text = fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
    if (file.endsWith('.svelte')) {
      text = [...text.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1]).join('\n');
    }
    return text.replace(/\/\*[\s\S]*?\*\//g, '');
  }

  function markUses(file: string, css: string): MarkUse[] {
    const found: MarkUse[] = [];
    for (const [, rawSelector = '', body = ''] of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
      let selector = rawSelector.trim();
      const prefix = DARK_PREFIXES.find((p) => selector.startsWith(p));
      if (prefix) selector = selector.slice(prefix.length);
      for (const decl of body.split(';')) {
        const [prop = '', ...rest] = decl.split(':');
        const value = rest.join(':');
        const token = value.match(HIGHLIGHT)?.[1];
        if (token === undefined) continue;
        const role = /^\s*background/.test(prop) ? 'fill' : 'ring';
        const percent = Number(value.match(PERCENT)?.[1] ?? 100);
        found.push({ file, selector, role, token, percent, dark: prefix !== undefined });
      }
    }
    return found;
  }

  const uses = SOURCES.flatMap((f) => markUses(f, styleText(f)));

  function themesOf(use: MarkUse): Theme[] {
    if (use.dark) return ['dark'];
    const overridden = uses.some(
      (o) => o.dark && o.file === use.file && o.selector === use.selector && o.role === use.role
    );
    return overridden ? ['light'] : BOTH;
  }

  function colourOf(theme: Theme, use: MarkUse) {
    const c = tokenColour(sets, theme, use.token);
    return { rgb: c.rgb, alpha: (c.alpha * use.percent) / 100 };
  }

  it('fills use the fill tokens and rings the ring tokens', () => {
    const wrong = uses
      .filter((u) => (u.role === 'ring') !== u.token.endsWith('-ring'))
      .map((u) => `${u.file} ${u.selector}: ${u.role} in ${u.token}`);
    expect(wrong).toEqual([]);
  });

  for (const use of uses) {
    for (const theme of themesOf(use)) {
      for (const surface of MARK_SURFACES) {
        const what = use.role === 'fill' ? '--text-primary on the fill' : 'the ring';
        it(`${theme}: ${use.selector} ${what} (${use.token} ${use.percent}%) over ${surface}`, () => {
          const under = surfaceRgb(theme, surface);
          const mark = compositeOver(colourOf(theme, use), under);
          if (use.role === 'fill') {
            const text = compositeOver(tokenColour(sets, theme, '--text-primary'), mark);
            expect(contrastRatio(text, mark)).toBeGreaterThanOrEqual(TEXT);
          } else {
            expect(contrastRatio(mark, under)).toBeGreaterThanOrEqual(NON_TEXT);
          }
        });
      }
    }
  }

  it('reads the marks it is meant to check (the scan is not vacuous)', () => {
    expect(uses.filter((u) => u.role === 'fill').length).toBeGreaterThanOrEqual(6);
    expect(uses.filter((u) => u.role === 'ring').length).toBeGreaterThanOrEqual(4);
    expect(uses.some((u) => u.file.endsWith('.svelte') && u.role === 'ring')).toBe(true);
    expect(uses.some((u) => themesOf(u).length === 2)).toBe(true);
  });

  it('control: the fills used as rings, and the old dark current mark, fail', () => {
    // Light: the yellow fill as a ring on white was 1.44:1. Dark: --text-primary on the current
    // mark at its old 59% over the dark card was 4.03:1.
    const white: Rgb = [255, 255, 255];
    const fill = tokenColour(sets, 'light', '--highlight-search');
    expect(contrastRatio(compositeOver(fill, white), white)).toBeLessThan(NON_TEXT);
    const card = surfaceRgb('dark', '--surface-color');
    const dark = tokenColour(sets, 'dark', '--highlight-search');
    const oldCurrent = compositeOver({ rgb: dark.rgb, alpha: (dark.alpha * 59) / 100 }, card);
    const text = tokenColour(sets, 'dark', '--text-primary').rgb;
    expect(contrastRatio(text, oldCurrent)).toBeLessThan(TEXT);
  });
});
