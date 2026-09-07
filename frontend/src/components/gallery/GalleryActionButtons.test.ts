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
  triggerTags: vi.fn(),
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
  await fireEvent.click(
    screen.getByTitle('Add to collection or export transcripts for selected files')
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  select([]);
});

describe('GalleryActionButtons — bulk tag entries', () => {
  it('offers one Tags entry in Organize, not separate add and remove', async () => {
    render(GalleryActionButtons, { props: { files: [] } });
    await openOrganizeMenu();

    // The modal does both add and remove (and, for a single file, the full chip
    // editor), so three doors to one dialog were three things to explain.
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add Tag' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Remove Tag' })).toBeNull();
  });

  it('names the tags entry for what it does to the selection', async () => {
    // A bare "Tags" beside "Add to Collection" did not say whether it added,
    // removed, or opened a manager. Both entries now describe the action they
    // perform on the selected files.
    render(GalleryActionButtons, { props: { files: [] } });
    await openOrganizeMenu();

    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Tags' })).toBeNull();
  });

  it('applies tags to the selection and closes the menu', async () => {
    select(['a']);
    render(GalleryActionButtons, {
      props: { files: [{ uuid: 'a', status: 'completed' }] as never },
    });
    await openOrganizeMenu();

    const tags = screen.getByRole('button', { name: 'Add or Edit Tags' });
    await fireEvent.click(tags);
    expect(gallery.triggerTags).toHaveBeenCalledTimes(1);
    // Choosing an entry closes the menu, like the neighbouring entries.
    expect(screen.queryByRole('button', { name: 'Add or Edit Tags' })).toBeNull();
  });

  it('disables both metadata entries when nothing is selected', async () => {
    // They act on the selection, so offering them with none invites the user to
    // "add to" or "edit" nothing. The toolbar's own Collections and Tags
    // buttons still open the managers, so nothing becomes unreachable.
    render(GalleryActionButtons, { props: { files: [] } });
    await openOrganizeMenu();

    expect(screen.getByRole('button', { name: 'Add to Collection' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).toBeDisabled();
  });

  it('enables them once a file is selected', async () => {
    select(['a']);
    render(GalleryActionButtons, {
      props: { files: [{ uuid: 'a', status: 'completed' }] as never },
    });
    await openOrganizeMenu();

    expect(screen.getByRole('button', { name: 'Add to Collection' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Add or Edit Tags' })).not.toBeDisabled();
  });
});

describe('GalleryActionButtons — actions that need a selection are disabled without one', () => {
  const completed = [{ uuid: 'a', status: 'completed' }] as never;

  it('disables every Organize export entry when nothing is selected', async () => {
    render(GalleryActionButtons, { props: { files: [] } });
    await openOrganizeMenu();

    // Export writes a transcript for each selected file. With none selected it
    // produced an empty download and no error — the Process menu already gates
    // its entries on the selection, so this read as one toolbar with two rules.
    for (const name of ['Export SRT', 'Export WebVTT', 'Export Text']) {
      expect(screen.getByRole('button', { name })).toBeDisabled();
    }
  });

  it('enables the export entries once a completed file is selected', async () => {
    // The control: same markup, same menu, opposite outcome driven only by the
    // selection — so the assertion above cannot be passing on a permanently
    // disabled button.
    select(['a']);
    render(GalleryActionButtons, { props: { files: completed } });
    await openOrganizeMenu();

    for (const name of ['Export SRT', 'Export WebVTT', 'Export Text']) {
      expect(screen.getByRole('button', { name })).not.toBeDisabled();
    }
  });

  it('leaves export disabled when the selected file has no transcript yet', async () => {
    select(['b']);
    render(GalleryActionButtons, {
      props: { files: [{ uuid: 'b', status: 'processing' }] as never },
    });
    await openOrganizeMenu();

    expect(screen.getByRole('button', { name: 'Export SRT' })).toBeDisabled();
  });

  it('disables Delete with no selection and enables it with one', async () => {
    const { unmount } = render(GalleryActionButtons, { props: { files: [] } });
    expect(screen.getByTitle('Permanently delete the selected files')).toBeDisabled();
    unmount();

    select(['a']);
    render(GalleryActionButtons, { props: { files: completed } });
    expect(screen.getByTitle('Permanently delete the selected files')).not.toBeDisabled();
  });
});
