/**
 * Translated labels are not cut off by a width chosen for English.
 *
 * Each of these was a fixed width or a whole-line ellipsis sized for the English string, and
 * each truncated in most other locales. jsdom measures no text, so each test pins the rule
 * that removes the cap; the measured check is a browser pass over the screen named in the test.
 *
 * Docs: MDN `text-overflow`, MDN `overflow-wrap`, MDN `minmax()`, CSS Flexbox Level 1 §4.5
 * (a flex item's automatic minimum size), WCAG 2.2 SC 1.4.10 Reflow (wrap rather than lose text).
 */
import fs from 'fs';
import { describe, it, expect } from 'vitest';
import { SRC, styleOf, atEveryWidth, declarations } from './sourceTree';

const style = (file: string) => styleOf(fs.readFileSync(`${SRC}/${file}`, 'utf8'));

describe('date range trigger in the filter sidebar', () => {
  const text = declarations(
    atEveryWidth(style('components/ui/DateRangePicker.svelte')),
    '.date-text'
  );

  it('wraps the label instead of ellipsizing it', () => {
    expect(text['white-space']).toBeUndefined();
    expect(text['text-overflow']).toBeUndefined();
    expect(text['overflow-wrap']).toBe('break-word');
    expect(text['min-width']).toBe('0');
  });
});

describe('gallery list status column', () => {
  const grid = style('components/gallery/file-list-grid.css');
  /** The last track of every `--file-list-columns` template (Status is always last). */
  const statusTracks = [...grid.matchAll(/--file-list-columns:\s*([^;]+);/g)].map((m) =>
    (m[1] ?? '')
      .trim()
      .split(/\s+(?![^(]*\))/)
      .pop()
  );

  it('reads every template (control)', () => {
    expect(statusTracks).toHaveLength(3);
  });

  it('gives status at least 140px wherever its label shows', () => {
    // Full and mid-width templates; the narrow one hides every column but title and status.
    expect(statusTracks[0]).toBe('minmax(140px, 1fr)');
    expect(statusTracks[1]).toBe('140px');
  });
});

describe('settings sidebar', () => {
  it('is wider than the 240px that wrapped section names in most locales', () => {
    const sidebar = declarations(
      atEveryWidth(style('components/settings/SettingsModalFrame.svelte')),
      '.settings-sidebar'
    );
    expect(Number.parseInt(sidebar.width ?? '', 10)).toBeGreaterThan(240);
  });
});

describe('gallery card meta line', () => {
  const css = atEveryWidth(style('components/gallery/VirtualGrid.svelte'));
  const markup = fs.readFileSync(`${SRC}/components/gallery/VirtualGrid.svelte`, 'utf8');

  it('shrinks only the file size, keeping the date and the speaker count whole', () => {
    expect(declarations(css, '.meta-text')['display']).toBe('flex');
    expect(declarations(css, '.meta-text > *')['flex-shrink']).toBe('0');
    const size = declarations(css, '.meta-text > .meta-size');
    expect(size['flex-shrink']).toBe('1');
    expect(size['min-width']).toBe('0');
    expect(size['text-overflow']).toBe('ellipsis');
  });

  it('puts the file size and its separator in the shrinking slot', () => {
    expect(markup).toMatch(
      /<span class="meta-size"><span class="meta-dot">&middot;<\/span><span dir="ltr">\{file\.formatted_file_size\}<\/span><\/span>/
    );
  });
});

describe('gallery count chip on a phone', () => {
  it('has no fixed width cap on its text on narrow screens', () => {
    const chip = style('components/gallery/GalleryCountChip.svelte');
    expect(chip).not.toMatch(/\{[^{}]*max-width:\s*\d+px/);
    expect(chip).toMatch(/\.count-chip\s*\{[^}]*min-width:\s*0;/);
  });
});
