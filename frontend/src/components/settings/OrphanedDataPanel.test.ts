/**
 * The admin "Orphaned data" panel (#1212). What matters is that nothing is deleted without
 * the confirmation the dialog describes, that a refused sweep is never forced without the
 * second confirmation, and that the destructive calls carry exactly the flags the user agreed
 * to.
 */
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';
import type { SearchOrphanReport, StorageOrphanReport } from '$lib/api/dataIntegrity';

vi.mock('$lib/api/dataIntegrity', () => ({
  getSearchOrphanCounts: vi.fn(),
  getStorageOrphanCounts: vi.fn(),
  getStorageSweepStatus: vi.fn(),
  removeSearchOrphans: vi.fn(),
  removeStorageOrphans: vi.fn(),
}));

vi.mock('$stores/locale', async () => {
  const { readable } = await import('svelte/store');
  const en = (await import('$lib/i18n/locales/en.json')).default as Record<string, string>;
  return {
    t: readable((key: string, opts?: Record<string, unknown>) => {
      let out = en[key] ?? key;
      for (const [name, value] of Object.entries(opts ?? {})) {
        out = out.split(`{{${name}}}`).join(String(value));
      }
      return out;
    }),
  };
});

vi.mock('$stores/toast', () => ({
  toastStore: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

import {
  getSearchOrphanCounts,
  getStorageOrphanCounts,
  getStorageSweepStatus,
  removeSearchOrphans,
  removeStorageOrphans,
} from '$lib/api/dataIntegrity';
import OrphanedDataPanel from './OrphanedDataPanel.svelte';

function searchReport(
  over: Partial<SearchOrphanReport['indices'][number]> = {}
): SearchOrphanReport {
  return {
    unavailable: false,
    summary: null,
    indices: [
      {
        name: 'transcripts',
        total_docs: 100,
        orphaned_docs: 4,
        deleted_docs: 0,
        refused: null,
        orphan_keys: [],
        orphan_keys_truncated: false,
        rechecked_present: 0,
        ...over,
      },
    ],
  };
}

function storageReport(over: Partial<StorageOrphanReport> = {}): StorageOrphanReport {
  return {
    dry_run: true,
    force: false,
    refused: null,
    error: null,
    total_objects: 100,
    total_bytes: 1000,
    orphan_files: 2,
    orphan_objects: 6,
    orphan_bytes: 3 * 1024 * 1024,
    orphans: [
      { file_id: 11, user_id: 1, objects: 3, bytes: 100 },
      { file_id: 12, user_id: 1, objects: 3, bytes: 100 },
    ],
    orphans_truncated: false,
    skipped_recent_files: 0,
    rechecked_present: 0,
    deleted_files: 0,
    deleted_objects: 0,
    deleted_bytes: 0,
    failed_objects: 0,
    ...over,
  };
}

function seed(search: SearchOrphanReport, storage: StorageOrphanReport) {
  vi.mocked(getSearchOrphanCounts).mockResolvedValue(search);
  vi.mocked(getStorageOrphanCounts).mockResolvedValue(storage);
}

const removeButton = () => screen.findByRole('button', { name: 'Remove orphaned data' });

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(removeSearchOrphans).mockResolvedValue({ status: 'started' });
  vi.mocked(removeStorageOrphans).mockResolvedValue({ status: 'started' });
  vi.mocked(getStorageSweepStatus).mockResolvedValue({ running: true, last_run: null });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('OrphanedDataPanel', () => {
  it('shows orphan counts and bytes for both search and storage', async () => {
    seed(searchReport(), storageReport());
    render(OrphanedDataPanel);

    expect(await screen.findByTestId('orphan-search-docs')).toHaveTextContent('4');
    expect(screen.getByTestId('orphan-storage-files')).toHaveTextContent('2');
    expect(screen.getByTestId('orphan-storage-size')).toHaveTextContent('3.0 MB');
    expect(screen.getByText(/11, 12/)).toBeInTheDocument();
  });

  it('is disabled and says why when nothing is orphaned', async () => {
    seed(
      searchReport({ orphaned_docs: 0 }),
      storageReport({ orphan_files: 0, orphan_objects: 0, orphan_bytes: 0, orphans: [] })
    );
    render(OrphanedDataPanel);

    expect(await removeButton()).toBeDisabled();
    expect(screen.getByTestId('orphan-none')).toBeInTheDocument();
  });

  it('deletes nothing until the dialog is confirmed, and the dialog names what goes', async () => {
    seed(searchReport(), storageReport());
    render(OrphanedDataPanel);

    await fireEvent.click(await removeButton());

    expect(
      await screen.findByText(
        /permanently deletes 4 orphaned search documents and 2 orphaned media files/
      )
    ).toBeInTheDocument();
    expect(removeSearchOrphans).not.toHaveBeenCalled();
    expect(removeStorageOrphans).not.toHaveBeenCalled();

    await fireEvent.click(screen.getByRole('button', { name: 'Remove permanently' }));

    await waitFor(() => expect(removeStorageOrphans).toHaveBeenCalledWith(false));
    expect(removeSearchOrphans).toHaveBeenCalledWith(false);
  });

  it('cancelling the dialog removes nothing', async () => {
    seed(searchReport(), storageReport());
    render(OrphanedDataPanel);

    await fireEvent.click(await removeButton());
    await fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));

    expect(removeSearchOrphans).not.toHaveBeenCalled();
    expect(removeStorageOrphans).not.toHaveBeenCalled();
  });

  it('explains a ratio-guard refusal and forces only after two confirmations', async () => {
    seed(
      searchReport({ orphaned_docs: 0 }),
      storageReport({ refused: 'ratio_guard', orphan_files: 30, orphan_objects: 30 })
    );
    render(OrphanedDataPanel);

    expect(await screen.findByTestId('orphan-guard')).toHaveTextContent('10%');
    // Everything found is held back, so the plain action stays disabled.
    expect(await removeButton()).toBeDisabled();

    await fireEvent.click(screen.getByRole('button', { name: 'I understand, remove anyway' }));
    await fireEvent.click(await screen.findByRole('button', { name: 'Continue' }));
    expect(removeStorageOrphans).not.toHaveBeenCalled();

    await fireEvent.click(await screen.findByRole('button', { name: 'Yes, remove anyway' }));

    await waitFor(() => expect(removeStorageOrphans).toHaveBeenCalledWith(true));
  });

  it('offers no override when the database holds no files', async () => {
    seed(searchReport({ orphaned_docs: 0 }), storageReport({ refused: 'empty_valid_set' }));
    render(OrphanedDataPanel);

    expect(await screen.findByTestId('orphan-empty-db')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'I understand, remove anyway' })).toBeNull();
    expect(await removeButton()).toBeDisabled();
  });

  it('reports a failed scan instead of an empty result', async () => {
    vi.mocked(getSearchOrphanCounts).mockRejectedValue(new Error('boom'));
    vi.mocked(getStorageOrphanCounts).mockResolvedValue(storageReport());
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    render(OrphanedDataPanel);

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not scan for orphaned data.');
    expect(screen.queryByTestId('orphan-none')).toBeNull();
    spy.mockRestore();
  });
});
