import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * The floating preview player (search and speakers pages) sits in the end corner. Written with
 * `right`/`margin-left`, it stayed bottom-right in Arabic, on top of the filter sidebar that RTL
 * moves to the right. jsdom cannot cascade scoped CSS, so this reads the style source and fails
 * on a physical left/right declaration (same check as `tags/rtlStyles.test.ts`).
 */
const FILE = path.resolve(__dirname, 'FloatingPreviewPlayer.svelte');

const PHYSICAL =
  /(?<![\w-])(?:(?:padding|margin|border)-(?:left|right)|left|right)\s*:|text-align:\s*(?:left|right)\b/g;

describe('FloatingPreviewPlayer uses logical properties so RTL mirrors', () => {
  it('has no physical left/right declarations', () => {
    const src = fs.readFileSync(FILE, 'utf8');
    const style = src.slice(src.indexOf('<style')).replace(/\/\*[\s\S]*?\*\//g, '');
    expect(style.match(PHYSICAL) ?? []).toEqual([]);
  });
});
