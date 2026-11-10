/**
 * Parse `theme.css` into token maps and resolve `var()` chains, so the contrast and parity
 * tests share ONE parser (`primary-contrast.test.ts` used to carry its own regexes).
 *
 * Imported by tests only. Not production code: it must stay out of the app bundle.
 */
import fs from 'fs';
import path from 'path';

export type Theme = 'light' | 'dark';
export type Rgb = [number, number, number];
export interface Colour {
  rgb: Rgb;
  alpha: number;
}

export interface TokenSets {
  /** Declarations in every `:root { }` block, later declarations winning. */
  root: Map<string, string>;
  /** Declarations in every `[data-theme='dark'] { }` block. */
  dark: Map<string, string>;
}

const BLOCK_SELECTORS: Record<string, keyof TokenSets> = {
  ':root': 'root',
  "[data-theme='dark']": 'dark',
};

function stripComments(css: string): string {
  return css.replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Split a declaration list on `;` that sit outside parentheses and quotes. */
function splitDeclarations(body: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let quote = '';
  let start = 0;
  for (let i = 0; i < body.length; i++) {
    const ch = body[i];
    if (quote) {
      if (ch === quote) quote = '';
    } else if (ch === '"' || ch === "'") {
      quote = ch;
    } else if (ch === '(') {
      depth++;
    } else if (ch === ')') {
      depth--;
    } else if (ch === ';' && depth === 0) {
      out.push(body.slice(start, i));
      start = i + 1;
    }
  }
  out.push(body.slice(start));
  return out;
}

/** Custom-property declarations from one block body. */
function declarations(body: string): Map<string, string> {
  const map = new Map<string, string>();
  for (const decl of splitDeclarations(body)) {
    const m = decl.match(/^\s*(--[\w-]+)\s*:\s*([\s\S]*?)\s*$/);
    const prop = m?.[1];
    const value = m?.[2];
    if (prop !== undefined && value !== undefined) map.set(prop, value.replace(/\s+/g, ' '));
  }
  return map;
}

// Folds a closed top-level block into its token set; blocks for other selectors are ignored.
function mergeBlock(sets: TokenSets, selector: string, body: string): void {
  const target = BLOCK_SELECTORS[selector];
  if (!target) return;
  for (const [k, v] of declarations(body)) sets[target].set(k, v);
}

/** Read the top-level `:root` and `[data-theme='dark']` blocks of a stylesheet. */
export function parseTokenSets(css: string): TokenSets {
  const text = stripComments(css);
  const sets: TokenSets = { root: new Map(), dark: new Map() };
  let depth = 0;
  let selectorStart = 0;
  let bodyStart = -1;
  let selector = '';
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '{') {
      if (depth === 0) {
        selector = text.slice(selectorStart, i).trim();
        bodyStart = i + 1;
      }
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0) {
        mergeBlock(sets, selector, text.slice(bodyStart, i));
        selectorStart = i + 1;
      }
    }
  }
  return sets;
}

const THEME_CSS_PATH = path.resolve(__dirname, 'theme.css');

export function loadThemeTokens(file: string = THEME_CSS_PATH): TokenSets {
  return parseTokenSets(fs.readFileSync(file, 'utf8'));
}

/** The raw declared value as written in ONE block, or undefined. */
export function declared(
  sets: TokenSets,
  block: 'root' | 'dark',
  name: string
): string | undefined {
  return sets[block].get(name);
}

/** The effective raw value for a theme: the dark block overrides `:root`. */
function effectiveRaw(sets: TokenSets, theme: Theme, name: string): string | undefined {
  if (theme === 'dark' && sets.dark.has(name)) return sets.dark.get(name);
  return sets.root.get(name);
}

const VAR_RE = /var\(\s*(--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\)[^()]*)*))?\)/;

/** Resolve every `var()` in a value under a theme. Throws on a cycle or an undefined token. */
function resolveValue(sets: TokenSets, theme: Theme, value: string, trail: string[] = []): string {
  let out = value;
  for (let guard = 0; guard < 50; guard++) {
    const m = out.match(VAR_RE);
    const name = m?.[1];
    if (!m || name === undefined) return out.trim();
    if (trail.includes(name)) throw new Error(`token cycle: ${[...trail, name].join(' -> ')}`);
    const raw = effectiveRaw(sets, theme, name);
    let replacement: string;
    if (raw !== undefined) replacement = resolveValue(sets, theme, raw, [...trail, name]);
    else if (m[2] !== undefined) replacement = resolveValue(sets, theme, m[2], trail);
    else throw new Error(`undefined token ${name} (theme ${theme})`);
    out = out.replace(m[0], replacement);
  }
  throw new Error(`could not resolve: ${value}`);
}

export function resolveToken(sets: TokenSets, theme: Theme, name: string): string {
  const raw = effectiveRaw(sets, theme, name);
  if (raw === undefined) throw new Error(`token ${name} is not declared (theme ${theme})`);
  return resolveValue(sets, theme, raw, [name]);
}

/** Parse a resolved colour: `#rgb`, `#rrggbb`, `#rrggbbaa`, `rgb(a)(r, g, b[, a])`, `white`. */
export function parseColour(value: string): Colour {
  const v = value.trim().toLowerCase();
  if (v === 'white') return { rgb: [255, 255, 255], alpha: 1 };
  const digits = v.match(/^#([0-9a-f]{3,8})$/)?.[1];
  if (digits !== undefined) {
    let h = digits;
    if (h.length === 3 || h.length === 4) h = [...h].map((c) => c + c).join('');
    if (h.length !== 6 && h.length !== 8) throw new Error(`bad hex colour: ${value}`);
    const n = (i: number) => parseInt(h.slice(i, i + 2), 16);
    return { rgb: [n(0), n(2), n(4)], alpha: h.length === 8 ? n(6) / 255 : 1 };
  }
  const fn = v.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)$/);
  if (fn) {
    return {
      rgb: [Number(fn[1]), Number(fn[2]), Number(fn[3])],
      alpha: fn[4] !== undefined ? Number(fn[4]) : 1,
    };
  }
  throw new Error(`cannot parse colour: ${value}`);
}

export function tokenColour(sets: TokenSets, theme: Theme, name: string): Colour {
  return parseColour(resolveToken(sets, theme, name));
}

/** Alpha-composite `fg` over an opaque `bg`. */
export function compositeOver(fg: Colour, bg: Rgb): Rgb {
  const mix = (back: number, front: number) => Math.round(back * (1 - fg.alpha) + front * fg.alpha);
  const [r, g, b] = fg.rgb;
  return [mix(bg[0], r), mix(bg[1], g), mix(bg[2], b)];
}

// WCAG 2.2 relative luminance (https://www.w3.org/TR/WCAG22/#dfn-relative-luminance). The spec
// changed 0.03928 to 0.04045 in May 2021; no 8-bit channel falls between them.
function relativeLuminance([r, g, b]: Rgb): number {
  const f = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
}

export function contrastRatio(a: Rgb, b: Rgb): number {
  const lumA = relativeLuminance(a);
  const lumB = relativeLuminance(b);
  const hi = Math.max(lumA, lumB);
  const lo = Math.min(lumA, lumB);
  return (hi + 0.05) / (lo + 0.05);
}

export function hexToRgb(hex: string): Rgb {
  return parseColour(hex).rgb;
}
