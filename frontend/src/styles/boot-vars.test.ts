/**
 * The pre-paint colours in `app.html` must equal the real theme tokens.
 *
 * `app.html` paints the page before any stylesheet loads (so a dark-theme user never sees a
 * white flash) using its own copies of three colours. They were the same values as
 * `theme.css`'s `--background-color`, `--text-color` and `--surface-color` by hand-copy, with
 * nothing to say so: a token edit would have left a flash of the OLD colour between first
 * paint and hydration.
 *
 * ⚠️ Not renamed (the styling-layer plan's R9 proposed `--boot-*`): `app.html` names them
 * `--bg-primary` / `--text-primary` / `--surface-primary`, and AFTER hydration `theme.css`
 * redefines `--bg-primary` as `--surface-color`, so `#app` and `.loading-placeholder` (which
 * read it) paint the SURFACE colour, not the page colour. A rename would repaint `#app` and is
 * therefore not a pixel-neutral Phase 0 change; it needs an owner decision.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';
import { loadThemeTokens, resolveToken, type Theme } from './themeTokens';

const html = fs.readFileSync(path.resolve(__dirname, '../app.html'), 'utf8');
const sets = loadThemeTokens();

/** `--name: value;` declarations inside the app.html block whose selector is `selector`. */
function bootBlock(selector: string): Record<string, string> {
  const start = html.indexOf(`${selector} {`);
  if (start === -1) throw new Error(`${selector} block not found in app.html`);
  const body = html.slice(html.indexOf('{', start) + 1, html.indexOf('}', start));
  return Object.fromEntries(
    [...body.matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim().toLowerCase()])
  );
}

const MAP: Array<[boot: string, token: string]> = [
  ['--bg-primary', '--background-color'],
  ['--text-primary', '--text-color'],
  ['--surface-primary', '--surface-color'],
];

describe('app.html pre-paint colours equal theme.css', () => {
  for (const [theme, selector] of [
    ['light', ':root'],
    ['dark', "[data-theme='dark']"],
  ] as Array<[Theme, string]>) {
    const boot = bootBlock(selector);
    for (const [bootName, tokenName] of MAP) {
      it(`${theme}: ${bootName} === ${tokenName}`, () => {
        expect(boot[bootName]).toBeDefined();
        expect(boot[bootName]).toBe(resolveToken(sets, theme, tokenName).toLowerCase());
      });
    }
  }
});
