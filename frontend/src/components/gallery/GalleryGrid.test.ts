import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string) => en[key] ?? key),
  };
});

/**
 * GalleryGrid reads `$stores/gallery` for the infinite-scroll/view-mode state,
 * which in the real store imports `$app/environment` — unresolvable under
 * vitest. Mocked the same way GallerySelectionActions.test.ts mocks $stores/gallery.
 */
vi.mock('$stores/gallery', async () => {
  const { readable } = await import('svelte/store');
  return {
    hasMoreFiles: readable(false),
    isLoadingMore: readable(false),
    galleryViewMode: readable('grid'),
  };
});

import GalleryGrid from './GalleryGrid.svelte';

const baseProps = {
  files: [],
  loading: false,
  error: null,
  selectedCollectionId: null,
  isSelecting: false,
  selectedFiles: new Set<string>(),
  pendingNewFiles: new Set<string>(),
  pendingDeletions: new Set<string>(),
};

describe('GalleryGrid — empty state distinguishes filtered-empty from library-empty (#747)', () => {
  it('shows the genuinely-empty-library message when no filter is active', () => {
    render(GalleryGrid, { props: { ...baseProps, filtersActive: false } });

    expect(screen.getByText('Your media library is empty.')).toBeInTheDocument();
    expect(
      screen.getByText('Use the uploader above to add your first media file!')
    ).toBeInTheDocument();
    expect(screen.queryByText('No files match')).toBeNull();
  });

  it('shows the filtered-to-zero message instead of "library is empty" when a filter is active', () => {
    // This is the claim issue #747 §4.1 makes: GalleryGrid used to always say the
    // library was empty, even with hundreds of files in it and one active filter.
    render(GalleryGrid, { props: { ...baseProps, filtersActive: true } });

    expect(screen.getByText('No files match')).toBeInTheDocument();
    expect(screen.getByText('Try adjusting or clearing your filters.')).toBeInTheDocument();
    expect(screen.queryByText('Your media library is empty.')).toBeNull();
  });

  it('a selected collection always wins, filtered or not', () => {
    render(GalleryGrid, {
      props: { ...baseProps, filtersActive: true, selectedCollectionId: 'some-uuid' },
    });

    expect(screen.getByText('No files in this collection.')).toBeInTheDocument();
    expect(screen.queryByText('No files match')).toBeNull();
  });
});
