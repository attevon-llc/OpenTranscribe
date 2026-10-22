/**
 * Hovering a menu item must not create a horizontal scrollbar. The menu is
 * overflow-y:auto (which makes overflow-x compute to auto), and items fill its
 * content width exactly, so any hover transform pushes them past the scrollport.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const src = readFileSync(resolve(__dirname, 'UserDropdown.svelte'), 'utf8');
const style = src.slice(src.indexOf('<style>')).replace(/\/\*[\s\S]*?\*\//g, '');

describe('UserDropdown item hover geometry', () => {
  it('uses no translate/transform on any dropdown item state', () => {
    expect(style).not.toMatch(/translate[XY]?\(/);
    expect(style).not.toMatch(/transform:\s*(?!none)\S/);
  });

  it('is direction-neutral (no physical text-align:left)', () => {
    expect(style).not.toMatch(/text-align:\s*left/);
  });
});
