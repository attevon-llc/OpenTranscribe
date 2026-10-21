/**
 * Repo rule: components import through the `$lib` / `$components` / `$stores` aliases, never
 * `../`-relative paths (same-folder `./` siblings are fine). These gallery-adjacent files
 * were flagged by the UX review audit; pinning them keeps the fix from regressing.
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, it, expect } from 'vitest';

const src = resolve(__dirname, '../..');
const files = [
  'components/gallery/GalleryPrimaryActions.svelte',
  'components/gallery/GallerySelectionActions.svelte',
  'routes/+page.svelte',
];

describe('alias imports', () => {
  it.each(files)('%s has no ../ relative imports', (file) => {
    const text = readFileSync(resolve(src, file), 'utf8');
    const relative = text.match(/^\s*import\s[^;\n]*from\s+['"]\.\.\/[^'"]*['"]/gm) ?? [];
    expect(relative).toEqual([]);
  });

  it('the scan really sees imports (control)', () => {
    const text = readFileSync(resolve(src, 'routes/+page.svelte'), 'utf8');
    expect(text.match(/^\s*import\s[^;\n]*from\s+['"]\$components\//gm)?.length).toBeGreaterThan(0);
  });
});
