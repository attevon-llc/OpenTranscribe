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
import {
  contrastRatio as contrast,
  declared,
  hexToRgb,
  loadThemeTokens,
  resolveToken,
} from './themeTokens';

// One parser for theme.css, shared with contrast-pairs.test.ts and token-parity.test.ts.
const sets = loadThemeTokens();

/** Resolves a token to a `#rrggbb` hex under the theme named by its selector. */
function token(selector: string, name: string): string {
  const value = resolveToken(sets, selector === ':root' ? 'light' : 'dark', name);
  if (!/^#[0-9a-fA-F]{6}$/.test(value)) throw new Error(`${name} under ${selector} is ${value}`);
  return value;
}

const blockOf = (selector: string) => (selector === ':root' ? 'root' : 'dark') as 'root' | 'dark';

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
      const triplet = declared(sets, blockOf(selector), '--primary-color-rgb')!;
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
      expect(declared(sets, blockOf(selector), '--primary-dark')).toBe('var(--primary-on-surface)');
    }
  });

  it('control: the previous values would have failed this gate', () => {
    // Proves the threshold discriminates rather than passing anything blue.
    expect(contrast(hexToRgb('#3b82f6'), WHITE)).toBeLessThan(AA_NORMAL_TEXT);
    expect(contrast(hexToRgb('#60a5fa'), WHITE)).toBeLessThan(AA_NORMAL_TEXT);
  });
});

/**
 * `.chip` in `components/upload/upload-shared.css` (rendered live by UploadStepCollections.svelte)
 * was a second contrast finding of the #835 investigation: `--primary-color` text on a dark chip is
 * about 2:1. That file is a plain stylesheet imported by script, so the `:global([data-theme='dark'])
 * .chip` override this test used to pin shipped verbatim and the browser dropped it as an invalid
 * selector: it never applied. The rule that styles the chip in dark is the base rule, through the
 * dark values of its tokens (`--primary-on-surface` on `--primary-bg`), so that is what is pinned.
 */
/**
 * `UploadProgress.svelte`'s per-file progress-bar fill (#752 gap 2) is a
 * NON-text UI component, so WCAG 2.1 SC 1.4.11 applies (3:1), not the 4.5:1
 * normal-text threshold used above. The fix reuses existing "-text"/"-dark"
 * status tokens rather than the plain `--success-color` / `--warning-color`
 * / etc. tokens, because the plain tokens do NOT clear 3:1 against
 * `--border-color` in light mode (see the control below) — this is exactly
 * the trap §12.4 of the plan warns about: verify the "before" number first
 * rather than assuming a token swap alone fixes the contrast.
 */
const NON_TEXT_AA = 3.0;

describe('progress fill clears non-text contrast (WCAG 1.4.11) against its track', () => {
  const progressStatusTokens: Record<string, string> = {
    completed: '--color-success-text',
    failed: '--error-dark',
    cancelled: '--text-secondary',
    active: '--color-info-text',
    pending: '--color-warning-text',
  };

  for (const [label, selector] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as const) {
    const border = hexToRgb(token(selector, '--border-color'));

    for (const [status, tokenName] of Object.entries(progressStatusTokens)) {
      it(`${label}: ${status} fill (${tokenName}) vs --border-color clears ${NON_TEXT_AA}:1`, () => {
        const fill = hexToRgb(token(selector, tokenName));
        expect(contrast(fill, border)).toBeGreaterThanOrEqual(NON_TEXT_AA);
      });
    }
  }

  it('control: the OLD hardcoded per-status hex values do not all clear 3:1 in light mode', () => {
    // These are the literals UploadProgress.svelte's getStatusColor() used to
    // return, checked against the LIGHT --border-color. Proves the gate
    // discriminates rather than passing anything already in the file.
    const lightBorder = hexToRgb(token(':root', '--border-color'));
    const oldHardcoded: Record<string, string> = {
      completed: '#10b981',
      pending: '#f59e0b', // the biggest offender: 1.73:1
    };
    for (const hex of Object.values(oldHardcoded)) {
      expect(contrast(hexToRgb(hex), lightBorder)).toBeLessThan(NON_TEXT_AA);
    }
  });
});

describe('.chip (upload-shared.css) clears AA contrast in dark mode', () => {
  const chipCss = fs.readFileSync(
    path.resolve(__dirname, '../components/upload/upload-shared.css'),
    'utf8'
  );

  function chipRuleBody(): string {
    const start = chipCss.search(/^\.chip \{/m);
    if (start === -1) throw new Error('.chip rule not found in upload-shared.css');
    return chipCss.slice(start, chipCss.indexOf('}', start));
  }

  const dark = "[data-theme='dark']";
  const chipBg = hexToRgb(token(dark, '--primary-bg'));

  it('colours its label and fill from tokens, so dark mode needs no override', () => {
    expect(chipRuleBody()).toMatch(/color:\s*var\(--primary-on-surface\)/);
    expect(chipRuleBody()).toMatch(/background:\s*var\(--primary-bg\)/);
    expect(chipCss).not.toMatch(/\.chip\s*\{[^}]*\}[\s\S]*\.chip\s*\{/);
  });

  it('--primary-on-surface on the dark --primary-bg clears AA', () => {
    const text = hexToRgb(token(dark, '--primary-on-surface'));
    expect(contrast(text, chipBg)).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
  });

  it('control: --primary-color text on that same fill fails AA', () => {
    // Proves the label token (not just the fill) is what clears the gate above.
    const oldText = hexToRgb(token(dark, '--primary-color'));
    expect(contrast(oldText, chipBg)).toBeLessThan(AA_NORMAL_TEXT);
  });
});

describe('error fill carrying white text (speakers inbox badge)', () => {
  // `--error-color` is the status FILL/icon colour: #ef4444 (3.76:1 with white) in light and
  // #f87171 (2.76:1) in dark. The /speakers inbox-count badge paints white text on it and
  // failed axe colour-contrast in both themes. A fill that carries white text is a different
  // role from the status colour, so it has its own token.
  for (const [label, selector] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as const) {
    it(`${label}: white on --error-solid clears ${AA_NORMAL_TEXT}:1`, () => {
      expect(contrast(hexToRgb(token(selector, '--error-solid')), WHITE)).toBeGreaterThanOrEqual(
        AA_NORMAL_TEXT
      );
    });

    it(`control: ${label}: white on --error-color (the fill it replaces) fails AA`, () => {
      expect(contrast(hexToRgb(token(selector, '--error-color')), WHITE)).toBeLessThan(
        AA_NORMAL_TEXT
      );
    });
  }
});

describe('upload tray header counts clear AA on the primary-blue header (UX review item 12)', () => {
  const tray = fs.readFileSync(
    path.resolve(__dirname, '../components/UploadManager.svelte'),
    'utf8'
  );

  /** Body of the rule whose (possibly comma-grouped) selector list names `selector`. */
  function rule(selector: string): string {
    const re = /([^{}]+)\{([^}]*)\}/g;
    for (const m of tray.matchAll(re)) {
      const names = m[1].split(',').map((s) => s.trim().split('\n').pop()!.trim());
      if (names.includes(selector)) return m[2];
    }
    throw new Error(`${selector} rule not found in UploadManager.svelte`);
  }

  /** Resolves a literal colour or `var(--token)` to RGB under one theme. */
  function resolveColour(value: string, selector: string): [number, number, number] {
    const v = value.trim();
    const viaVar = v.match(/^var\((--[\w-]+)/);
    if (viaVar) return hexToRgb(token(selector, viaVar[1]));
    if (v === 'white') return WHITE;
    const hex = v.match(/^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/);
    if (!hex) throw new Error(`cannot resolve colour: ${value}`);
    const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join('') : hex[1];
    return hexToRgb('#' + h);
  }

  const headerBg = rule('.panel-header').match(/background:\s*([^;]+);/)![1];
  const headerFg = rule('.panel-header').match(/\bcolor:\s*([^;]+);/)![1];

  for (const [label, selector] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as const) {
    for (const cls of ['.stat-active', '.stat-queued', '.stat-completed']) {
      it(`${label}: ${cls} text clears ${AA_NORMAL_TEXT}:1 on its background`, () => {
        const body = rule(cls);
        const fg = body.match(/\bcolor:\s*([^;]+);/)?.[1] ?? headerFg;
        const bg = body.match(/background:\s*([^;]+);/)?.[1] ?? headerBg;
        const ratio = contrast(resolveColour(fg, selector), resolveColour(bg, selector));
        expect(ratio).toBeGreaterThanOrEqual(AA_NORMAL_TEXT);
      });
    }
  }

  it('the active count is the boldest of the three', () => {
    expect(rule('.stat-active')).toMatch(
      /font-weight:\s*(?:[6-9]00|var\(--font-weight-semibold\))/
    );
  });

  it('control: the previous amber / grey / green literals fail on the header blue', () => {
    const header = hexToRgb(token(':root', '--primary-color'));
    for (const hex of ['#fbbf24', '#d1d5db', '#86efac']) {
      expect(contrast(hexToRgb(hex), header)).toBeLessThan(AA_NORMAL_TEXT);
    }
  });
});
