import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * The gallery's duration slider (`svelte-range-slider-pips`) places its handles with a physical
 * `translate` from the left edge and has no right-to-left mode. Under an inherited `dir="rtl"`
 * the handles start from the right edge instead, so in Arabic the maximum handle sat 222 px
 * outside the slider and could not be reached. The scale is pinned left to right, like the
 * player's sliders (`player/player.css`), and the min/max labels above it with it, so the labels
 * still name the handle beneath them. jsdom cannot lay out, so this reads the style source.
 */
const FILE = path.resolve(__dirname, 'FilterSidebar.svelte');

function rule(style: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return style.match(new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`))?.[1] ?? '';
}

describe('FilterSidebar duration slider in right-to-left languages', () => {
  const src = fs.readFileSync(FILE, 'utf8');
  const style = src.slice(src.indexOf('<style'));

  it('keeps the scale itself left to right', () => {
    expect(rule(style, '.slider-wrapper')).toMatch(/direction:\s*ltr/);
  });

  it('lets the label row follow the document direction so min sits over the reversed min handle', () => {
    expect(rule(style, '.slider-labels')).not.toMatch(/direction:/);
    expect(src).toMatch(/reversed=\{getLanguageDirection\(\$locale\) === 'rtl'\}/);
  });

  it('isolates each label so its digits stay left to right', () => {
    const r = rule(style, '.slider-labels span');
    expect(r).toMatch(/direction:\s*ltr/);
    expect(r).toMatch(/unicode-bidi:\s*isolate/);
  });
});
