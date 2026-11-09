/**
 * The computed "Legal hold" tag chip (legal-hold plan §5, Lane D): shown only while the caller
 * can see a held file (`held_count > 0`), it drives `held=true` through the gallery store, and
 * it offers nothing to rename, delete or remove.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { writable } from 'svelte/store';

vi.mock('$stores/locale', async () =>
  (await import('$testMocks/galleryToolbarMocks')).englishLocale()
);
const h = vi.hoisted(() => ({ store: null as unknown, setHeldFilter: vi.fn(), metadata: vi.fn() }));
vi.mock('$stores/gallery', () => {
  h.store = writable({ filterHeld: false });
  return { galleryState: h.store, galleryStore: { setHeldFilter: h.setHeldFilter } };
});
vi.mock('$lib/api/metadataFilters', () => ({ fetchMetadataFilters: h.metadata }));

import GalleryHeldFilter from './GalleryHeldFilter.svelte';
import { heldCount } from '$lib/legalHold/heldCount';

const galleryState = () => h.store as ReturnType<typeof writable<{ filterHeld: boolean }>>;

beforeEach(() => {
  vi.clearAllMocks();
  heldCount.set(0);
  galleryState().set({ filterHeld: false });
});

describe('GalleryHeldFilter', () => {
  it('stays hidden while the caller can see no held file', async () => {
    h.metadata.mockResolvedValue({ held_count: 0 });
    render(GalleryHeldFilter);
    await vi.waitFor(() => expect(h.metadata).toHaveBeenCalled());
    expect(screen.queryByTestId('held-tag-filter')).toBeNull();
  });

  it('appears with the count once held_count is above zero, and turns held=true on', async () => {
    h.metadata.mockResolvedValue({ held_count: 3 });
    render(GalleryHeldFilter);
    const chip = await screen.findByRole('button', { name: /Legal hold/ });
    expect(chip).toHaveTextContent('3');
    expect(chip).toHaveAttribute('aria-pressed', 'false');

    await fireEvent.click(chip);
    expect(h.setHeldFilter).toHaveBeenCalledWith(true);
  });

  it('shows on, and turns off, while the filter is applied', async () => {
    h.metadata.mockResolvedValue({ held_count: 2 });
    galleryState().set({ filterHeld: true });
    render(GalleryHeldFilter);
    const chip = await screen.findByRole('button', { name: /Legal hold/ });
    expect(chip).toHaveAttribute('aria-pressed', 'true');
    await fireEvent.click(chip);
    expect(h.setHeldFilter).toHaveBeenCalledWith(false);
  });

  it('is locked: one toggle, no edit, delete or remove control', async () => {
    h.metadata.mockResolvedValue({ held_count: 1 });
    render(GalleryHeldFilter);
    await screen.findByRole('button', { name: /Legal hold/ });
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });
});
