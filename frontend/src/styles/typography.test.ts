/**
 * Typography guards (issue #1243, owner review item 12: "a lot of bold or heavy weight font").
 *
 * Two pure-source defects, pinned here because jsdom neither loads web fonts nor resolves
 * logical CSS properties, so no render test can observe either:
 *
 * 1. `app.html` declared `@font-face { font-weight: 600 }` with the **Bold** file and weight
 *    300 with a file `scripts/download-fonts.js` never writes. Every `font-weight: 600` in the
 *    app (~330 rules — the documented "semibold for headings and emphasis") therefore drew the
 *    700 glyphs, which is most of why chat and search read as all-bold.
 * 2. The chat prose layer and the search snippets used physical `left` properties, so in the
 *    `ar` (RTL) locale list markers were clipped off the right edge and blockquote/semantic
 *    accents sat on the trailing side.
 */
import { describe, it, expect } from 'vitest';
import fs from 'fs';
import path from 'path';

const FRONTEND = path.resolve(__dirname, '..', '..');
const read = (rel: string) => fs.readFileSync(path.join(FRONTEND, rel), 'utf8');

/** weight -> file basename, as `download-fonts.js` writes them (`Poppins-<Name>.woff2`). */
function downloadedFontFiles(): Map<string, string> {
  const src = read('scripts/download-fonts.js');
  const map = new Map<string, string>();
  for (const m of src.matchAll(/(\d{3}):\s*\{\s*name:\s*'([A-Za-z]+)'/g)) {
    map.set(m[1], `Poppins-${m[2]}.woff2`);
  }
  return map;
}

/** weight -> file basename, as `app.html`'s @font-face rules reference them. */
function declaredFontFaces(): Map<string, string> {
  const html = read('src/app.html');
  const map = new Map<string, string>();
  for (const block of html.matchAll(/@font-face\s*\{([^}]*)\}/g)) {
    const weight = block[1].match(/font-weight:\s*(\d{3})/)?.[1];
    const file = block[1].match(/url\('\/fonts\/([^']+)'\)/)?.[1];
    if (weight && file) map.set(weight, file);
  }
  return map;
}

describe('Poppins @font-face mapping', () => {
  it('parses both sides (guards against a regex that matches nothing)', () => {
    expect(downloadedFontFiles().size).toBeGreaterThanOrEqual(5);
    expect(declaredFontFaces().size).toBeGreaterThanOrEqual(5);
  });

  it('serves every declared weight from the file downloaded for THAT weight', () => {
    const downloaded = downloadedFontFiles();
    const mismatched = [...declaredFontFaces()]
      .filter(([weight, file]) => downloaded.get(weight) !== file)
      .map(([weight, file]) => `${weight}: app.html=${file} download=${downloaded.get(weight)}`);
    expect(mismatched).toEqual([]);
  });
});

/** The `<style>` block of a Svelte component, comments stripped. */
function styleOf(rel: string): string {
  const src = read(rel);
  const style = src.slice(src.indexOf('<style'));
  return style.replace(/\/\*[\s\S]*?\*\//g, '');
}

const PHYSICAL =
  /\b(padding-left|padding-right|margin-left|margin-right|border-left|border-right)\s*:|text-align:\s*(left|right)\b/g;

describe('nothing is heavier than semibold', () => {
  // The heaviest weight in the UI is 600 (`--font-weight-semibold`); 700/bold resolves to Poppins
  // Bold, which is most of why pages read as all-bold (owner review item 12).
  const HEAVY = /font-weight:\s*(?:[789]00|bold(?:er)?)\b/g;
  const ALLOW: Record<string, string> = {
    'components/LoginBanner.svelte':
      'classification marking: security banners are bold-caps by regulation convention, not UI emphasis',
    'lib/components/ClassificationBanner.svelte':
      'classification marking: security banners are bold-caps by regulation convention, not UI emphasis',
  };
  const SRC = path.resolve(FRONTEND, 'src');
  const files: string[] = [];
  const walk = (dir: string) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (/\.(svelte|css)$/.test(e.name)) files.push(p);
    }
  };
  walk(SRC);
  const hits = new Map<string, number>();
  for (const f of files) {
    const css = fs.readFileSync(f, 'utf8').replace(/\/\*[\s\S]*?\*\//g, '');
    const n = (css.match(HEAVY) ?? []).length;
    if (n) hits.set(path.relative(SRC, f).split(path.sep).join('/'), n);
  }

  it('uses --font-weight-semibold (600) as the heaviest weight', () => {
    expect([...hits].filter(([f]) => !(f in ALLOW)).map(([f, n]) => `${f} (${n})`)).toEqual([]);
  });

  it('has no stale allowlist entries', () => {
    expect(Object.keys(ALLOW).filter((f) => !hits.has(f))).toEqual([]);
  });
});

describe('RTL-safe chat prose and search snippets', () => {
  const FILES = [
    'src/components/chat/ChatMarkdown.svelte',
    'src/components/chat/ChatMessage.svelte',
    'src/components/chat/ChatSources.svelte',
    'src/components/search/SearchOccurrence.svelte',
    'src/components/search/SearchResultCard.svelte',
  ];

  it.each(FILES)('%s uses logical (inline-start/end) properties only', (rel) => {
    expect(styleOf(rel).match(PHYSICAL) ?? []).toEqual([]);
  });
});
