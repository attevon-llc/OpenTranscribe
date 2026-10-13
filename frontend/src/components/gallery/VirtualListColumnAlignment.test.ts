/**
 * Column alignment of the gallery list view (issue #1243, item 14).
 *
 * In selection mode every header label sat 24px to the right of its data column (and
 * mirrored wrongly in RTL): the header was a grid on `--file-list-columns-selecting` with
 * its own 1rem padding before the checkbox track, while a row was `40px 1fr` with a
 * differently padded, physically-padded link. Two hand-tuned geometries that only agreed
 * by accident in normal mode.
 *
 * jsdom cannot cascade Svelte's scoped CSS, so, like VirtualListHeaderStyle.test.ts, this
 * reads the style sources and pins the structure that makes disagreement impossible: header,
 * skeleton and rows all consume ONE template, and the link is a subgrid of the row. The
 * measured proof (header vs cell left edges, real browser) is
 * backend/tests/e2e/test_gallery_list_alignment.py.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const read = (f: string) => readFileSync(resolve(__dirname, f), 'utf8');
const list = read('VirtualList.svelte');
const listStyle = list.slice(list.indexOf('<style'));
const skeleton = read('FileListSkeleton.svelte');
const skeletonStyle = skeleton.slice(skeleton.indexOf('<style'));
const grid = read('file-list-grid.css');

function ruleBody(css: string, selector: string): string | null {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = new RegExp(`(?:^|\\n)\\s*${escaped}\\s*\\{([^}]*)\\}`).exec(css);
  return match ? match[1] : null;
}

describe('gallery list column alignment', () => {
  it('derives the selecting template from the shared columns, once', () => {
    expect(grid).toMatch(
      /--file-list-columns-selecting:\s*var\(--file-list-check\)\s+var\(--file-list-columns\)/
    );
    // No per-breakpoint copy of the selecting template: that duplication is how it drifted.
    expect(grid.match(/--file-list-columns-selecting:/g)).toHaveLength(1);
  });

  it('lays a selecting row out on the same template as the header', () => {
    const row = ruleBody(listStyle, '.file-list-row.selecting-mode');
    expect(row).not.toBeNull();
    expect(row).toMatch(/grid-template-columns:\s*var\(--file-list-columns-selecting\)/);
    expect(row).toMatch(/padding-inline:\s*var\(--file-list-pad\)/);
    expect(row).toMatch(/column-gap:\s*var\(--file-list-gap\)/);
    expect(ruleBody(listStyle, '.file-list-header.selecting-mode')).toMatch(
      /grid-template-columns:\s*var\(--file-list-columns-selecting\)/
    );
  });

  it('makes the selecting link a subgrid over the data tracks, with no padding of its own', () => {
    const link = ruleBody(listStyle, '.file-list-row.selecting-mode .file-list-link');
    expect(link).not.toBeNull();
    expect(link).toMatch(/grid-template-columns:\s*subgrid/);
    expect(link).toMatch(/grid-column:\s*2\s*\/\s*-1/);
    expect(link).toMatch(/padding-inline:\s*0/);
  });

  it('uses no physical left/right padding that would not mirror in RTL', () => {
    expect(listStyle).not.toMatch(/padding:\s*0\.5rem\s+1rem\s+0\.5rem\s+0\b/);
    expect(listStyle).not.toMatch(/padding:\s*0\.5rem\s+0\s+0\.5rem\s+1rem\b/);
  });

  it('shares outer padding and gap between header, link and skeleton', () => {
    for (const [css, selectors] of [
      [listStyle, ['.file-list-header', '.file-list-link']],
      [skeletonStyle, ['.file-list-header', '.skel-row']],
    ] as const) {
      for (const selector of selectors) {
        const body = ruleBody(css, selector);
        expect(body, selector).not.toBeNull();
        expect(body, selector).toMatch(/gap:\s*var\(--file-list-gap\)/);
        expect(body, selector).toMatch(/padding:[^;]*var\(--file-list-pad\)/);
      }
    }
  });

  it('gives the skeleton selecting rows the checkbox track too', () => {
    expect(skeletonStyle).toMatch(
      /\.skel-row\.selecting-mode\s*\{[^}]*var\(--file-list-columns-selecting\)/
    );
    expect(skeleton).toMatch(/class="skel-row"\s+class:selecting-mode=\{isSelecting\}/);
  });
});
