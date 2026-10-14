/**
 * Slider drift guards.
 *
 * Contract: every native <input type="range"> in a Svelte template goes through
 * `ui/RangeSlider.svelte`, and its look lives in `styles/range-slider.css`. Before
 * this, five hand-rolled sliders in the settings sections each had their own track,
 * thumb and value readout, and only one of them was the design the owner wanted.
 *
 * Both guards are allowlist-gated: an entry needs a written reason, and an entry that
 * no longer matches anything fails (a stale exemption is silent permission).
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'fs';
import { join, relative, resolve } from 'path';

const SRC = resolve(__dirname, '..', '..');

/** `file (relative to src/) -> why it may hold its own range input / thumb rules`. */
const RAW_INPUT_ALLOWLIST: Record<string, string> = {};

const THUMB_RULE_ALLOWLIST: Record<string, string> = {
  'styles/range-slider.css': 'the central slider stylesheet',
  'components/VideoPlayer.svelte':
    'Plyr generates its inputs at runtime; only vendor pseudo-element selectors can style them',
  'components/PlyrMiniPlayer.svelte':
    'Plyr generates its inputs at runtime; only vendor pseudo-element selectors can style them',
};

const SELF = 'components/ui/RangeSlider.svelte';

/** Strip <style> blocks so CSS selectors like `input[type="range"]` are not markup. */
function stripStyle(source: string): string {
  return source.replace(/<style[\s\S]*?<\/style>/g, '');
}

/**
 * True when the markup contains an <input> whose type attribute is "range". Scans
 * tag by tag, skipping `{...}` expressions, because attribute expressions such as
 * `on:change={(e) => ...}` contain a literal `>` that defeats a naive `[^>]*` regex.
 */
export function hasRawRangeInput(source: string): boolean {
  const markup = stripStyle(source);
  const open = /<input\b/g;
  let m: RegExpExecArray | null;
  while ((m = open.exec(markup))) {
    let i = m.index + m[0].length;
    let depth = 0;
    let quote = '';
    let tag = '';
    for (; i < markup.length; i++) {
      const c = markup[i];
      if (depth === 0 && quote) {
        if (c === quote) quote = '';
      } else if (depth === 0 && (c === '"' || c === "'")) {
        quote = c;
      } else if (c === '{') {
        depth++;
      } else if (c === '}') {
        depth--;
      } else if (c === '>' && depth === 0 && !quote) {
        break;
      }
      tag += c;
    }
    // Drop expression bodies so `{type}`-style noise cannot fake a match.
    const attrs = tag.replace(/\{[^}]*\}/g, '{}');
    if (/(^|\s)type\s*=\s*["']?range["']?(\s|$|\/)/.test(attrs)) return true;
  }
  return false;
}

const THUMB_RULE = /::-webkit-slider-thumb|::-moz-range-thumb/;

export function hasThumbRule(source: string): boolean {
  return THUMB_RULE.test(source);
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) {
      if (name === 'node_modules') continue;
      walk(p, out);
    } else out.push(p);
  }
  return out;
}

const files = walk(SRC).map((p) => ({ rel: relative(SRC, p), path: p }));
const svelte = files.filter((f) => f.rel.endsWith('.svelte'));
const styled = files.filter((f) => /\.(svelte|css)$/.test(f.rel));

describe('hasRawRangeInput (detector self-test)', () => {
  it('must fire on a raw range input, including a multi-line tag with `=>` in a handler', () => {
    expect(hasRawRangeInput('<input type="range" min="0" />')).toBe(true);
    expect(
      hasRawRangeInput(
        `<input
          id="x"
          on:change={(e) => go((e.target as HTMLInputElement).value)}
          type='range'
        />`
      )
    ).toBe(true);
  });

  it('must stay clean on non-range inputs, CSS selectors and prose', () => {
    expect(hasRawRangeInput('<input type="number" />')).toBe(false);
    expect(hasRawRangeInput('<input type="checkbox" bind:checked />')).toBe(false);
    expect(
      hasRawRangeInput('<style>:global(.plyr input[type="range"]) { color: red }</style>')
    ).toBe(false);
    expect(hasRawRangeInput('<RangeSlider id="a" label="b" bind:value />')).toBe(false);
    expect(hasRawRangeInput('<input class="range-input" placeholder="type range" />')).toBe(false);
  });
});

describe('hasThumbRule (detector self-test)', () => {
  it('must fire on both vendor thumb pseudo-elements', () => {
    expect(hasThumbRule('.a::-webkit-slider-thumb { width: 1px }')).toBe(true);
    expect(hasThumbRule('.a::-moz-range-thumb { width: 1px }')).toBe(true);
  });
  it('must stay clean on ordinary CSS and on the track pseudo-elements', () => {
    expect(hasThumbRule('.a { accent-color: red }')).toBe(false);
    expect(hasThumbRule('.a::-webkit-slider-runnable-track { height: 1px }')).toBe(false);
  });
});

describe('slider structure', () => {
  it('scans a non-trivial number of files (the guard is not vacuous)', () => {
    expect(svelte.length).toBeGreaterThan(100);
    expect(svelte.some((f) => f.rel === SELF)).toBe(true);
  });

  it('RangeSlider itself is the one place that renders type="range"', () => {
    const self = svelte.find((f) => f.rel === SELF)!;
    expect(hasRawRangeInput(readFileSync(self.path, 'utf8'))).toBe(true);
  });

  it('no other .svelte template holds a raw type="range" input', () => {
    const offenders = svelte
      .filter((f) => f.rel !== SELF && !(f.rel in RAW_INPUT_ALLOWLIST))
      .filter((f) => hasRawRangeInput(readFileSync(f.path, 'utf8')))
      .map((f) => f.rel);
    expect(offenders, 'use <RangeSlider> from $components/ui/RangeSlider.svelte').toEqual([]);
  });

  it('raw-input allowlist entries carry a reason and still match', () => {
    for (const [file, reason] of Object.entries(RAW_INPUT_ALLOWLIST)) {
      expect(reason.trim().length, file).toBeGreaterThan(10);
      expect(hasRawRangeInput(readFileSync(join(SRC, file), 'utf8')), file).toBe(true);
    }
  });

  it('thumb pseudo-element rules live only in the central stylesheet and the player files', () => {
    const offenders = styled
      .filter((f) => !(f.rel in THUMB_RULE_ALLOWLIST))
      .filter((f) => hasThumbRule(readFileSync(f.path, 'utf8')))
      .map((f) => f.rel);
    expect(offenders, 'put slider styling in styles/range-slider.css').toEqual([]);
  });

  it('thumb-rule allowlist entries carry a reason and the files exist', () => {
    for (const [file, reason] of Object.entries(THUMB_RULE_ALLOWLIST)) {
      expect(reason.trim().length, file).toBeGreaterThan(10);
      expect(() => statSync(join(SRC, file)), file).not.toThrow();
    }
  });
});
