/**
 * Theme-parity guards (issue #746 — icon buttons invisible in one theme).
 *
 * Two defects were found by source audit and are pinned here, because both are
 * pure-CSS failures that no rendering test in jsdom can observe (jsdom does not
 * implement the cascade well enough to compute `getComputedStyle` across an
 * external stylesheet plus Svelte-scoped component CSS).
 *
 * 1. The theme is applied as `data-theme="dark"` on <html> plus `theme-dark` on
 *    <body> (see `src/stores/theme.js` and `static/theme.js`). No element in the
 *    app is ever given the class `dark`, so every `:global(.dark) …` rule a
 *    component wrote as its dark-mode override was dead code: the light-mode
 *    value stayed applied in dark mode.
 *
 * 2. `form-elements.css` repainted `background-color` on `button:focus`. A
 *    browser puts `:focus` on a <button> on mouse-down, so the repaint stuck
 *    after every click, and the selector's specificity (0,2,1) beats the
 *    (0,2,0) that Svelte emits for a scoped single-class rule (`.foo.svelte-HASH`).
 *    Icon buttons styled `.foo { background: <solid>; color: white }` therefore
 *    lost their background once clicked and became a white glyph on
 *    `var(--button-hover)` over a white surface.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const SRC = path.resolve(__dirname, '..');

function svelteFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) svelteFiles(p, out);
    else if (entry.name.endsWith('.svelte')) out.push(p);
  }
  return out;
}

/**
 * Files still carrying the dead selector. They are owned by concurrent branches
 * and could not be edited from the #746 branch without a guaranteed conflict.
 * The list is asserted to be exact in BOTH directions below, so it cannot rot
 * silently: removing the selector from one of these files fails the test until
 * the entry is deleted too.
 */
const KNOWN_DEAD_DARK_SELECTOR_FILES = [
  'components/FileUploader.svelte',
  'components/search/SearchTranscriptModal.svelte',
  // `routes/+page.svelte` left this list when its hand-rolled modal chrome was
  // replaced by BaseModal (#739): the dead `:global(.dark)` rules went with it.
];

describe('dark-mode selector convention', () => {
  const offenders = svelteFiles(SRC)
    .filter((f) => fs.readFileSync(f, 'utf8').includes(':global(.dark)'))
    .map((f) => path.relative(SRC, f).split(path.sep).join('/'))
    .sort();

  it('uses [data-theme=dark] — `.dark` is never applied to any element', () => {
    expect(offenders).toEqual([...KNOWN_DEAD_DARK_SELECTOR_FILES].sort());
  });

  it('has at least one migrated component, so the convention is actually in use', () => {
    const migrated = svelteFiles(SRC).filter((f) =>
      fs.readFileSync(f, 'utf8').includes(":global([data-theme='dark'])")
    );
    expect(migrated.length).toBeGreaterThan(20);
  });
});

describe('global button styling', () => {
  const css = fs.readFileSync(path.join(SRC, 'styles', 'form-elements.css'), 'utf8');
  // Comments are stripped before rule-splitting: the explanatory note added for
  // #746 quotes the removed selector, and a naive split would read the prose as
  // part of the next rule's selector and re-report the very defect it documents.
  const cssNoComments = css.replace(/\/\*[\s\S]*?\*\//g, '');

  it('never repaints a bare button background on :focus', () => {
    const rules = cssNoComments.match(/([^{}]+)\{([^{}]*)\}/g) ?? [];
    const offending = rules.filter((rule) => {
      const [selector, body] = [rule.slice(0, rule.indexOf('{')), rule.slice(rule.indexOf('{'))];
      if (!/\bbutton\b[^,{]*:focus\b/.test(selector)) return false;
      if (/:focus-visible/.test(selector)) return false;
      return /background(-color)?\s*:/.test(body);
    });
    expect(offending).toEqual([]);
  });

  it('still gives keyboard users a visible focus ring', () => {
    expect(css).toMatch(/button:focus-visible\s*\{[^}]*outline\s*:/);
  });

  it('keeps the hover tint on :hover', () => {
    expect(css).toMatch(
      /button:hover:not\(:disabled\)\s*\{[^}]*background-color:\s*var\(--button-hover\)/
    );
  });
});
