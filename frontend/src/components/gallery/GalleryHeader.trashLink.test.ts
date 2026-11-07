/**
 * Issue #1101: deleting a file moves it to the trash, so the gallery toolbar carries the way
 * back to it. A plain link (an `<a href>`, so it opens in a new tab and is announced as a
 * link) in the unconditional second row, present whatever the file list holds.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
vi.mock('$stores/supportSession', async () => {
  const { readable } = await import('svelte/store');
  return { supportSessionGate: readable({ active: false, readOnly: false }) };
});

import GalleryHeader from './GalleryHeader.svelte';

describe('GalleryHeader trash link', () => {
  it('links to /trash from the toolbar, even with no files', () => {
    render(GalleryHeader, {
      props: {
        files: [],
        sortBy: 'upload_time',
        sortOrder: 'desc',
        loading: false,
        showFilters: false,
      },
    });
    const link = screen.getByRole('link', { name: 'Trash' });
    expect(link.getAttribute('href')).toBe('/trash');
    expect(link.closest('.toolbar-row-bottom')).not.toBeNull();
  });
});
