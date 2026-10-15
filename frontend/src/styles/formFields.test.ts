/**
 * One look for every text field (#1244 items 5 and 6f).
 *
 * `styles/form-elements.css` is the only place that decides how an <input>, <textarea> or
 * <select> looks (border, radius, background, focus ring, chevron, placeholder, disabled,
 * invalid, light/dark). Before it was, 320 component rules re-declared those per field, with
 * five different radii (3, 4, 6, 8, 12px) and four different focus treatments, and a bare
 * `input { width: 100% }` stretched a checkbox across its row.
 *
 * Two checks:
 *  - the shared sheet keeps its structural promises (scoped to text-like fields, zero
 *    specificity so a deliberate component exception still wins, an `appearance: none`
 *    select with one chevron mirrored in RTL, theme-aware `color-scheme`);
 *  - no component re-paints a field it renders. Layout (width, flex, margin) and size
 *    (padding, font-size) are the component's; paint is not. Embedded fields that sit
 *    inside a wrapper which draws the box (`border: none` / transparent) are fine, and a
 *    genuine exception goes in ALLOWED with a written reason (stale entries fail).
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..');
const css = readFileSync(join(__dirname, 'form-elements.css'), 'utf8');

/** `file::selector -> why this field may paint itself`. */
const ALLOWED: Record<string, string> = {};

const PAINT =
  /^\s*(border(?:-color|-radius|-width|-style)?|background(?:-color|-image)?|box-shadow|outline(?:-offset|-color)?)\s*:/m;
const NON_TEXT = /type=['"]?(checkbox|radio|range|file|color|hidden|button|submit|image|reset)/;

function walk(dir: string, out: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (full.endsWith('.svelte')) out.push(full);
  }
  return out;
}

/** Classes on text-like <input>/<textarea>/<select> elements (brace-aware: `=>` contains `>`). */
export function fieldClasses(markup: string): Set<string> {
  const classes = new Set<string>();
  const tag = /<(input|textarea|select)\b/g;
  let m: RegExpExecArray | null;
  while ((m = tag.exec(markup))) {
    let depth = 0;
    let j = m.index + m[0].length;
    for (; j < markup.length; j++) {
      const ch = markup[j];
      if (ch === '{') depth++;
      else if (ch === '}') depth = Math.max(0, depth - 1);
      else if (ch === '>' && depth === 0) break;
    }
    const attrs = markup.slice(m.index + m[0].length, j);
    if (NON_TEXT.test(attrs)) continue;
    for (const c of attrs.matchAll(/class="([^"]*)"/g)) {
      for (const name of c[1].split(/\s+/)) if (name && !name.includes('{')) classes.add(name);
    }
    for (const c of attrs.matchAll(/class:([\w-]+)/g)) classes.add(c[1]);
  }
  return classes;
}

function lastCompound(selector: string): string {
  const plain = selector
    .replace(/:global\(([^)]*)\)/g, '$1')
    .replace(/(:focus-visible|:focus-within|:focus|:hover|:disabled|:active|:read-only)+$/, '')
    .trim();
  return plain.split(/[\s>+~]+/).pop() ?? '';
}

/** Selectors in a component's <style> that paint a text field it renders. */
export function paintedFields(source: string): string[] {
  const style = /<style[^>]*>([\s\S]*)<\/style>/.exec(source)?.[1] ?? '';
  const markup = source.slice(0, source.indexOf('<style'));
  const classes = fieldClasses(markup);
  const hits: string[] = [];
  for (const rule of style.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = rule[1].trim().split('\n').pop()!.trim();
    const body = rule[2];
    if (selector.startsWith('@') || selector.includes('::')) continue;
    // `outline: none` on an embedded field is how it defers to its wrapper's focus ring.
    if (!PAINT.test(body.replace(/^\s*outline\s*:\s*(none|0)\s*;/gm, ''))) continue;
    // A field embedded in a wrapper that draws the box is deliberately bare.
    if (/^\s*border\s*:\s*(none|0)\b/m.test(body)) continue;
    if (/^\s*background(-color)?\s*:\s*(transparent|none)\b/m.test(body)) continue;
    const parts = selector.split(',').map((p) => p.trim());
    const targetsField = parts.some((part) => {
      const lc = lastCompound(part);
      const tagMatch = /^(input|textarea|select)(\[[^\]]*\])?(\.[\w-]+)*$/.exec(lc);
      if (tagMatch) return !(tagMatch[2] && NON_TEXT.test(tagMatch[2]));
      const names = /^((?:\.[\w-]+)+)$/.exec(lc)?.[1].split('.').slice(1) ?? [];
      return names.some((n) => classes.has(n));
    });
    if (targetsField) hits.push(selector);
  }
  return hits;
}

describe('paintedFields (the detector itself)', () => {
  const comp = (style: string, markup = '<input class="f" />') =>
    `${markup}<style>${style}</style>`;

  it('fires on a re-painted input class', () => {
    expect(paintedFields(comp('.f { border: 1px solid red; border-radius: 3px; }'))).toEqual([
      '.f',
    ]);
  });

  it('fires on a bare tag selector and on a focus ring', () => {
    expect(paintedFields(comp('.row input { background: #fff; }'))).toEqual(['.row input']);
    expect(paintedFields(comp('.f:focus { box-shadow: 0 0 0 2px blue; }'))).toEqual(['.f:focus']);
  });

  it('sees a select whose handler contains an arrow function', () => {
    const markup = `<select class="s" on:change={() => go()}></select>`;
    expect(paintedFields(comp('.s { border-radius: 6px; }', markup))).toEqual(['.s']);
  });

  it('stays quiet for layout and size, embedded fields, and non-text inputs', () => {
    expect(
      paintedFields(comp('.f { width: 100%; flex: 1; padding: 0.25rem; font-size: 0.8rem; }'))
    ).toEqual([]);
    expect(
      paintedFields(comp('.f { border: none; background: transparent; outline: none; }'))
    ).toEqual([]);
    expect(
      paintedFields(comp('.c { border-radius: 4px; }', '<input type="checkbox" class="c" />'))
    ).toEqual([]);
    expect(paintedFields(comp('.card { border: 1px solid red; }'))).toEqual([]);
  });
});

describe('form-elements.css structure', () => {
  it('scopes the shared field rule to text-like inputs, so a checkbox is never stretched', () => {
    expect(css).not.toMatch(/\ninput,\s*\ntextarea\s*\{/);
    const start = css.indexOf(':where(\n    input:not(');
    expect(start).toBeGreaterThan(-1);
    const selector = css.slice(start, css.indexOf('{', start));
    for (const type of [
      'checkbox',
      'radio',
      'range',
      'file',
      'color',
      'hidden',
      'button',
      'submit',
    ]) {
      expect(selector).toContain(`[type='${type}']`);
    }
  });

  it('keeps every field state at zero specificity so a component exception still wins', () => {
    const fieldRules = css.split('\n}\n').filter((r) => r.includes('input:not('));
    expect(fieldRules.length).toBeGreaterThan(4);
    for (const rule of fieldRules) {
      expect(rule.trimStart().startsWith(':where(')).toBe(true);
    }
  });

  it('makes select identical on every platform: no native chrome, one chevron, mirrored in RTL', () => {
    expect(css).toMatch(/-webkit-appearance:\s*none/);
    expect(css).toMatch(/appearance:\s*none/);
    expect(css).toContain('--field-chevron');
    expect(css).toMatch(/\[dir='rtl'\]\s*:where\(select\)\s*\{[^}]*background-position:\s*left/);
    expect(css).toMatch(/padding-inline-end/);
    expect(css).toContain('::-ms-expand');
  });

  it('lets the browser draw its own widgets in the app theme', () => {
    expect(css).toMatch(/:root\s*\{[\s\S]*?color-scheme:\s*light/);
    expect(css).toMatch(/\[data-theme='dark'\]\s*\{[\s\S]*?color-scheme:\s*dark/);
  });

  it('never falls back to the OS control font', () => {
    expect(css).toMatch(/font-family:\s*inherit/);
  });

  it('defines focus, invalid, disabled and placeholder states', () => {
    expect(css).toContain('where(:focus)');
    expect(css).toContain("[aria-invalid='true']");
    expect(css).toContain(':disabled');
    expect(css).toContain('::placeholder');
  });

  it('normalises date, time and search chrome that differs between macOS and Windows', () => {
    expect(css).toContain('::-webkit-date-and-time-value');
    expect(css).toContain('::-webkit-calendar-picker-indicator');
    expect(css).toContain('::-webkit-search-cancel-button');
    expect(css).toContain(':-webkit-autofill');
  });
});

describe('components do not re-paint the fields they render', () => {
  const files = walk(SRC).map((f) => ({ rel: relative(SRC, f).replace(/\\/g, '/'), full: f }));

  it('scans the component tree', () => {
    expect(files.length).toBeGreaterThan(100);
  });

  it('has no ad-hoc border, radius, background, shadow or outline on a text field', () => {
    const found: string[] = [];
    for (const f of files) {
      for (const selector of paintedFields(readFileSync(f.full, 'utf8'))) {
        const key = `${f.rel}::${selector}`;
        if (!(key in ALLOWED)) found.push(key);
      }
    }
    expect(
      found,
      'Delete the declaration: styles/form-elements.css already paints every field. Keep only layout and size.'
    ).toEqual([]);
  });

  it('every allowlist entry still matches something', () => {
    const live = new Set<string>();
    for (const f of files) {
      for (const selector of paintedFields(readFileSync(f.full, 'utf8')))
        live.add(`${f.rel}::${selector}`);
    }
    expect(Object.keys(ALLOWED).filter((k) => !live.has(k))).toEqual([]);
  });
});
