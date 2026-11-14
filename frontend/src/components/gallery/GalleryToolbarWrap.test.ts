/**
 * The gallery toolbar never lets one control slide under another.
 *
 * At 1280px the selection toolbar in de/es/fr/it/nl/pt/ru was wider than the content column.
 * The rows only wrapped below 1200px, so above that the selection buttons ran under the trash
 * link and the sort control, and "Delete N selected" could not be clicked (en: the cancel button
 * sat under the trash link). jsdom computes no layout, so this pins the rules that prevent it:
 * the rows and the selection group wrap at EVERY width, and the trailing group keeps to the
 * inline end when it drops to its own line. The measured check is a browser pass over
 * the gallery in selection mode, every locale.
 *
 * Docs: MDN `flex-wrap`, MDN `margin-inline-start` (auto margins in flex layout).
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';
import { atEveryWidth, declarations, styleOf } from '$styles/sourceTree';

const base = (file: string) =>
  atEveryWidth(styleOf(readFileSync(resolve(__dirname, file), 'utf8')));
const header = base('GalleryHeader.svelte');
const selection = base('GallerySelectionActions.svelte');

describe('gallery toolbar wraps instead of overlapping', () => {
  it('wraps both toolbar rows at every width', () => {
    expect(declarations(header, '.toolbar-row')['flex-wrap']).toBe('wrap');
  });

  it('keeps the trash, sort and view group at the inline end when it wraps', () => {
    expect(declarations(header, '.toolbar-row-right')['margin-inline-start']).toBe('auto');
  });

  it('wraps the selection actions at every width and lets their wrapper shrink', () => {
    expect(declarations(selection, '.selection-actions')['flex-wrap']).toBe('wrap');
    expect(declarations(selection, '.gallery-action-buttons')['min-width']).toBe('0');
  });
});
