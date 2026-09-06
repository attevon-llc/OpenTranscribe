/**
 * White text on the primary blue must clear WCAG 2.1 AA (4.5:1).
 *
 * `--primary-color` is the background of every primary button, active chip,
 * selected tab and the user avatar — all of which carry WHITE text. It was
 * `#3b82f6` in light and `#60a5fa` in dark, giving 3.68:1 and 2.54:1: fine for
 * a large control, under the bar for the label sizes actually used, and two
 * different blues to keep in step. Measured live across the gallery, search,
 * speakers, chat and File Status pages, that single token accounted for every
 * remaining contrast finding in BOTH themes.
 *
 * The ratio is asserted from the stylesheet rather than eyeballed, because the
 * failure mode is a token edit made for an unrelated reason quietly dropping
 * back under the threshold — invisible in review, and jsdom cannot compute it
 * from a rendered page.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const css = fs.readFileSync(path.resolve(__dirname, 'theme.css'), 'utf8');

function hexToRgb(hex: string): [number, number, number] {
  const h = hex.replace('#', '');
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)) as [number, number, number];
}

function relativeLuminance([r, g, b]: [number, number, number]): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

/** Reads a custom property out of a specific selector block. */
function token(selector: string, name: string): string {
  const block = css.slice(css.indexOf(selector));
  const m = block.match(new RegExp(`${name}:\\s*(#[0-9a-fA-F]{6})`));
  if (!m) throw new Error(`${name} not found under ${selector}`);
  return m[1];
}

const WHITE: [number, number, number] = [255, 255, 255];
const AA_NORMAL_TEXT = 4.5;

describe('primary blue carries white text at AA contrast', () => {
  for (const [label, selector] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as const) {
    it(`${label}: white on --primary-color clears ${AA_NORMAL_TEXT}:1`, () => {
      const ratio = contrast(hexToRgb(token(selector, '--primary-color')), WHITE);
      expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
    });

    it(`${label}: --primary-hover stays at least as readable as the base`, () => {
      // Hover darkens (Apple HIG), so it can only improve white-on-blue. A
      // hover that went lighter would fail the user exactly while they are
      // interacting with the control.
      const base = contrast(hexToRgb(token(selector, '--primary-color')), WHITE);
      const hover = contrast(hexToRgb(token(selector, '--primary-hover')), WHITE);
      expect(hover).toBeGreaterThanOrEqual(base);
    });
  }

  it('uses ONE primary blue across both themes', () => {
    // Two blues meant two things to keep in sync, and they had already drifted
    // to different contrast ratios (3.68 vs 2.54).
    expect(token("[data-theme='dark']", '--primary-color')).toBe(token(':root', '--primary-color'));
  });

  it('--primary-color-rgb matches --primary-color in both themes', () => {
    // Components tint with `rgba(var(--primary-color-rgb), α)`. When the hex and
    // the triplet disagree, solid fills and tints of "the same" colour diverge —
    // the exact bug the status-colour triplets above them were added to fix.
    for (const selector of [':root', "[data-theme='dark']"]) {
      const block = css.slice(css.indexOf(selector));
      const triplet = block.match(/--primary-color-rgb:\s*([\d\s,]+);/)![1];
      const parsed = triplet.split(',').map((n) => Number(n.trim()));
      expect(parsed).toEqual(hexToRgb(token(selector, '--primary-color')));
    }
  });

  it('--primary-dark is an alias, not a second copy of the same colour', () => {
    // It held #1d4ed8 / #93c5fd — byte-identical to --primary-on-surface, for
    // the identical job — under a name that reads as a lie in dark mode, where
    // "dark" resolves to a LIGHT blue. Two copies of one value drift; one of
    // them was already the reason this theme carried two primary blues.
    for (const selector of [':root', "[data-theme='dark']"]) {
      const block = css.slice(css.indexOf(selector));
      const m = block.match(/--primary-dark:\s*([^;]+);/)!;
      expect(m[1].trim()).toBe('var(--primary-on-surface)');
    }
  });

  it('control: the previous values would have failed this gate', () => {
    // Proves the threshold discriminates rather than passing anything blue.
    expect(contrast(hexToRgb('#3b82f6'), WHITE)).toBeLessThan(AA_NORMAL_TEXT);
    expect(contrast(hexToRgb('#60a5fa'), WHITE)).toBeLessThan(AA_NORMAL_TEXT);
  });
});
