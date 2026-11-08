/**
 * Under the computed "Legal hold" entry the panel printed "No collections yet", which read as a
 * contradiction. The empty state stays, under a "Your collections" subheading, and only when
 * the entry is shown (manage mode with a hold handler).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../test-mocks/galleryToolbarMocks')).englishLocale()
);
vi.mock('$lib/axios', () => ({
  default: { get: vi.fn().mockResolvedValue({ data: [] }), post: vi.fn(), delete: vi.fn() },
}));
vi.mock('$lib/api/sharing', () => ({
  SharingApi: { fetchSharedCollections: vi.fn().mockResolvedValue([]) },
}));
vi.mock('$lib/api/collections', () => ({ CollectionsApi: {} }));
vi.mock('$lib/legalHold/heldCount', async () => {
  const { writable } = await import('svelte/store');
  return { heldCount: writable(3), refreshHeldCount: vi.fn() };
});

import CollectionsPanel from './CollectionsPanel.svelte';

describe('CollectionsPanel empty state beside the Legal hold entry', () => {
  it("labels the empty state as the person's own collections", async () => {
    const { findByTestId, getByText } = render(CollectionsPanel, {
      props: { viewMode: 'manage', onHeldSelect: () => {} },
    });
    const heading = await findByTestId('own-collections-heading');
    expect(heading).toHaveTextContent('Your collections');
    expect(getByText('No collections yet')).toBeInTheDocument();
  });

  it('has no subheading when there is no Legal hold entry above it', async () => {
    const { queryByTestId, findByText } = render(CollectionsPanel, {
      props: { viewMode: 'manage' },
    });
    await findByText('No collections yet');
    await waitFor(() => expect(queryByTestId('own-collections-heading')).toBeNull());
  });
});
