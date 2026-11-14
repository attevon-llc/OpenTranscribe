/**
 * Source-tree scan helpers for the style-source tests (jsdom applies no cascade, so these tests
 * read `.svelte` / `.css` / `.ts` files directly).
 */
import fs from 'fs';
import path from 'path';

/** `frontend/src`. */
export const SRC = path.resolve(__dirname, '..');

/** Every file under `dir` whose name matches `include`, skipping directories named in `skipDirs`. */
export function walkSource(
  dir: string,
  include: RegExp,
  options: { exclude?: RegExp; skipDirs?: string[] } = {},
  out: string[] = []
): string[] {
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (!options.skipDirs?.includes(e.name)) walkSource(p, include, options, out);
    } else if (include.test(e.name) && !options.exclude?.test(e.name)) out.push(p);
  }
  return out;
}

/** `p` relative to `frontend/src`, with forward slashes. */
export const rel = (p: string) => path.relative(SRC, p).split(path.sep).join('/');

/** The `<style>` blocks of a `.svelte` source (a `.css` source is returned whole), comments stripped. */
export function styleOf(source: string): string {
  const blocks = [...source.matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((m) => m[1] ?? '');
  return (blocks.length ? blocks.join('\n') : source).replace(/\/\*[\s\S]*?\*\//g, '');
}

/**
 * `css` with every at-rule block (`@media`, `@container`, `@supports`, `@keyframes`) cut out,
 * leaving only the rules that apply at every width. Brace-counted, so nested blocks go whole.
 */
export function atEveryWidth(css: string): string {
  let out = '';
  let i = 0;
  while (i < css.length) {
    const at = css.indexOf('@', i);
    if (at === -1) return out + css.slice(i);
    out += css.slice(i, at);
    const open = css.indexOf('{', at);
    if (open === -1) return out;
    let depth = 1;
    let j = open + 1;
    while (j < css.length && depth > 0) {
      const c = css.charAt(j);
      if (c === '{') depth += 1;
      else if (c === '}') depth -= 1;
      j += 1;
    }
    i = j;
  }
  return out;
}

/** One innermost `selector { body }` pair; rules nested in at-rules are included. */
export interface CssRule {
  selector: string;
  body: string;
}

/** Every innermost rule of `css`, selector whitespace collapsed. */
export function cssRules(css: string): CssRule[] {
  return [...css.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: (m[1] ?? '').trim().replace(/\s+/g, ' '),
    body: m[2] ?? '',
  }));
}

/** Declarations (`prop` -> value) of every rule whose selector is exactly `selector`, merged in order. */
export function declarations(css: string, selector: string): Record<string, string> {
  const merged: Record<string, string> = {};
  for (const rule of cssRules(css)) {
    if (rule.selector !== selector) continue;
    for (const decl of rule.body.split(';')) {
      const colon = decl.indexOf(':');
      if (colon > 0) merged[decl.slice(0, colon).trim()] = decl.slice(colon + 1).trim();
    }
  }
  return merged;
}
