/**
 * The gallery's "Hidden files" chips (issue #1102): Leave out / Include / Only hidden.
 *
 * The mode lives in the gallery store; a chip asks the store to change it (which reloads the
 * list), and the selected chip follows the store, so a reset back to "Leave out" shows.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';
import { writable } from 'svelte/store';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
const state = vi.hoisted(() => ({ store: null as unknown }));
const setHiddenMode = vi.hoisted(() => vi.fn());
vi.mock('$stores/gallery', () => {
  state.store = writable({ filterHiddenMode: 'exclude' });
  return { galleryState: state.store, galleryStore: { setHiddenMode } };
});

import GalleryHiddenFilter from './GalleryHiddenFilter.svelte';

const galleryState = () => state.store as ReturnType<typeof writable<{ filterHiddenMode: string }>>;

beforeEach(() => {
  vi.clearAllMocks();
  galleryState().set({ filterHiddenMode: 'exclude' });
});

describe('GalleryHiddenFilter', () => {
  it('starts on "Leave out" and says hiding affects nobody else', () => {
    render(GalleryHiddenFilter);
    expect(screen.getByRole('button', { name: 'Leave out' })).toHaveAttribute(
      'aria-pressed',
      'true'
    );
    expect(screen.getByRole('button', { name: 'Only hidden' })).toHaveAttribute(
      'aria-pressed',
      'false'
    );
    expect(screen.getByText(/never affects anyone else/)).toBeInTheDocument();
  });

  it('asks the store for the chosen mode, and only when it changes', async () => {
    render(GalleryHiddenFilter);
    await fireEvent.click(screen.getByRole('button', { name: 'Only hidden' }));
    await fireEvent.click(screen.getByRole('button', { name: 'Leave out' }));
    expect(setHiddenMode.mock.calls).toEqual([['only']]);
  });

  it('follows the store, so a reset shows "Leave out" again', async () => {
    galleryState().set({ filterHiddenMode: 'include' });
    render(GalleryHiddenFilter);
    expect(screen.getByRole('button', { name: 'Include' })).toHaveAttribute('aria-pressed', 'true');

    galleryState().set({ filterHiddenMode: 'exclude' });

    expect(
      await screen.findByRole('button', { name: 'Leave out', pressed: true })
    ).toBeInTheDocument();
  });
});
