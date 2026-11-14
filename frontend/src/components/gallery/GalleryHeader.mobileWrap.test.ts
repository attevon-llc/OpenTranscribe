import { describe, it, expect } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * UI pass O6: at 390px the empty-gallery count chip shared one nowrap row with "Select files"
 * and was ellipsized in fr and nl. jsdom cannot lay out, so this reads the style source: the
 * <=768px rule for `.toolbar-row-left` must let its items wrap.
 */
const src = fs.readFileSync(path.resolve(__dirname, 'GalleryHeader.svelte'), 'utf8');
const mobile = src.slice(src.indexOf('@media (max-width: 768px)'));

describe('GalleryHeader mobile toolbar', () => {
  it('lets the left group wrap so a long count chip gets its own line', () => {
    const body = mobile.match(/\.toolbar-row-left\s*\{([^}]*)\}/)?.[1] ?? '';
    expect(body).toMatch(/flex-wrap:\s*wrap/);
  });
});
