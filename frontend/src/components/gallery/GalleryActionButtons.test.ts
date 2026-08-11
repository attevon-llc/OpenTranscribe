import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/svelte';

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      // Mirror i18next's plural resolution: a `count` option selects the
      // `_one` / `_other` variant. Without it, pluralized keys fall through to
      // the raw key and every assertion on that copy fails for the wrong reason.
      const count = opts?.count;
      const plural =
        typeof count === 'number' ? (count === 1 ? `${key}_one` : `${key}_other`) : undefined;
      let out = (plural && en[plural]) ?? en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

/**
 * The toolbar reads the shared selection straight from `$stores/gallery`, which
 * imports `$app/environment` — unresolvable under vitest. Mocking the store both
 * fixes that and lets a test drive the selection size directly.
 */
const gallery = vi.hoisted(() => ({
  triggerAddTags: vi.fn(),
  triggerRemoveTags: vi.fn(),
  triggerAddToCollection: vi.fn(),
  triggerExport: vi.fn(),
  triggerUpload: vi.fn(),
  triggerCollections: vi.fn(),
  triggerDeleteSelected: vi.fn(),
  triggerReprocess: vi.fn(),
  triggerSummarize: vi.fn(),
  triggerRedact: vi.fn(),
  triggerRetryFailed: vi.fn(),
  triggerSpeakerId: vi.fn(),
  triggerCancelProcessing: vi.fn(),
  setSelecting: vi.fn(),
  selectAllFiles: vi.fn(),
  clearSelection: vi.fn(),
}));

const stores = vi.hoisted(async () => {
  const { writable } = await import('svelte/store');
  return {
    selectedCount: writable(0),
    galleryState: writable({ isSelecting: true, selectedFiles: new Set<string>() }),
  };
});

vi.mock('$stores/gallery', async () => {
  const { readable } = await import('svelte/store');
  const { selectedCount, galleryState } = await stores;
  return {
    galleryStore: gallery,
    galleryState,
    selectedCount,
    allFilesSelected: readable(false),
  };
});

const { selectedCount, galleryState } = await stores;

import GalleryActionButtons from './GalleryActionButtons.svelte';

function select(uuids: string[]) {
  selectedCount.set(uuids.length);
  galleryState.set({ isSelecting: true, selectedFiles: new Set(uuids) });
}

async function openOrganizeMenu() {
  await fireEvent.click(screen.getByTitle('Add to collection or export transcripts for selected files'));
}

beforeEach(() => {
  vi.clearAllMocks();
  select([]);
});

describe('GalleryActionButtons — bulk tag entries', () => {
  it('disables both tag entries while nothing is selected', async () => {
    render(GalleryActionButtons, { props: { files: [] } });
    await openOrganizeMenu();

    expect(screen.getByRole('button', { name: 'Add Tag' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Remove Tag' })).toBeDisabled();
  });

  it('fires the store triggers once files are selected', async () => {
    select(['file-1', 'file-2']);
    render(GalleryActionButtons, { props: { files: [] } });

    await openOrganizeMenu();
    await fireEvent.click(screen.getByRole('button', { name: 'Add Tag' }));
    expect(gallery.triggerAddTags).toHaveBeenCalledTimes(1);

    // Choosing an entry closes the menu, exactly like the neighbouring entries.
    expect(screen.queryByRole('button', { name: 'Add Tag' })).toBeNull();

    await openOrganizeMenu();
    await fireEvent.click(screen.getByRole('button', { name: 'Remove Tag' }));
    expect(gallery.triggerRemoveTags).toHaveBeenCalledTimes(1);
  });
});
