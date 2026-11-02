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
  ...['--surface-color', '--background-color'].map((bg) => ({
    fg: '--text-secondary',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'secondary text on the two base surfaces only (4.34:1 on --hover-color in light)',
  })),
  // The documented near-miss: secondary text on a hover or sunken surface uses --text-on-tint.
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
  ...['--warning-solid', '--warning-solid-hover'].map((bg) => ({
    fg: '--text-on-warning',
    bg,
    min: TEXT,
    themes: BOTH,
    reason: 'dark label on the amber fill (white is 2.15:1)',
  })),
  // non-text
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
