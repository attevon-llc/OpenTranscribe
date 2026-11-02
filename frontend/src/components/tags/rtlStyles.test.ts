import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * The tag surfaces must mirror in `ar` (RTL). jsdom cannot cascade scoped CSS,
 * so this reads each component's style source and fails on a physical
 * left/right declaration: indents, input icons and clear buttons written with
 * `left`/`right` stay on the left in RTL and overlap the start of the text.
 */
const SRC = path.resolve(__dirname, '..', '..');

const FILES = [
  'components/tags/TagList.svelte',
  'components/tags/TagManagerModal.svelte',
  'components/TagsEditor.svelte',
];

/** The `<style>` block of a Svelte component, comments stripped. */
function styleOf(rel: string): string {
  const src = fs.readFileSync(path.join(SRC, rel), 'utf8');
  return src.slice(src.indexOf('<style')).replace(/\/\*[\s\S]*?\*\//g, '');
}

/** Physical side properties, `left`/`right` offsets and text alignment. */
const PHYSICAL =
  /(?<![\w-])(?:(?:padding|margin|border)-(?:left|right)|left|right)\s*:|text-align:\s*(?:left|right)\b/g;

/** A four-value padding/margin shorthand whose two inline sides differ. */
function asymmetricShorthands(style: string): string[] {
  const found: string[] = [];
  for (const m of style.matchAll(/(?<![\w-])(padding|margin)\s*:\s*([^;}]+)/g)) {
    const parts = m[2].trim().split(/\s+/);
    if (parts.length === 4 && parts[1] !== parts[3]) found.push(m[0].trim());
  }
  return found;
}

describe('tag surfaces use logical properties so RTL mirrors', () => {
  it.each(FILES)('%s has no physical left/right declarations', (rel) => {
    const style = styleOf(rel);
    expect(style.match(PHYSICAL) ?? []).toEqual([]);
    expect(asymmetricShorthands(style)).toEqual([]);
  });
});
