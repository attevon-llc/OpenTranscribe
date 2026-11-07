/**
 * Bulk "Hide from my view" / "Show in my view" in the Organize menu (issue #1102).
 *
 * Each entry acts only on the files it would change, is disabled when it would change none,
 * reports what happened, and tells the gallery to reload: a hidden file leaves the default list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/svelte';

vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);
vi.mock('$stores/gallery', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).galleryStoreMock()
);
const setHiddenMany = vi.hoisted(() => vi.fn());
vi.mock('$lib/api/hiddenMedia', () => ({
  HiddenMediaApi: { setHiddenMany },
  HIDDEN_LIMIT_ERROR: 'HIDDEN_LIMIT',
}));
const toast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: toast }));

import GallerySelectionActions from './GallerySelectionActions.svelte';

const { galleryTriggers, resetGalleryDouble, selectFiles } = await import(
  '../../test-mocks/galleryToolbarMocks'
);

type Row = { uuid: string; status: string; is_hidden?: boolean };

async function openOrganize(files: Row[]) {
  render(GallerySelectionActions, { props: { files: files as never } });
  await fireEvent.click(
    screen.getByTitle('Add to collection or export transcripts for selected files')
  );
}

beforeEach(resetGalleryDouble);

describe('GallerySelectionActions: hide and show (#1102)', () => {
  it('disables both entries with nothing selected', async () => {
    await openOrganize([]);
    expect(screen.getByRole('button', { name: 'Hide from my view' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Show in my view' })).toBeDisabled();
  });

  it('offers only the entry that would change the selection', async () => {
    selectFiles(['a']);
    await openOrganize([{ uuid: 'a', status: 'completed', is_hidden: false }]);
    expect(screen.getByRole('button', { name: 'Hide from my view' })).not.toBeDisabled();
    expect(screen.getByRole('button', { name: 'Show in my view' })).toBeDisabled();
  });

  it('hides only the files not hidden yet, reports it and reloads the gallery', async () => {
    setHiddenMany.mockResolvedValue([{ file_uuid: 'a', success: true, message: 'File hidden' }]);
    selectFiles(['a', 'b']);
    await openOrganize([
      { uuid: 'a', status: 'completed', is_hidden: false },
      { uuid: 'b', status: 'completed', is_hidden: true },
    ]);

    await fireEvent.click(screen.getByRole('button', { name: 'Hide from my view' }));

    await waitFor(() => expect(galleryTriggers.notifyHiddenChanged).toHaveBeenCalledTimes(1));
    expect(setHiddenMany).toHaveBeenCalledWith(['a'], true);
    expect(toast.success).toHaveBeenCalledWith('Hidden from your view: 1');
    expect(toast.error).not.toHaveBeenCalled();
  });

  it('shows hidden files again', async () => {
    setHiddenMany.mockResolvedValue([{ file_uuid: 'b', success: true, message: 'File unhidden' }]);
    selectFiles(['b']);
    await openOrganize([{ uuid: 'b', status: 'completed', is_hidden: true }]);

    await fireEvent.click(screen.getByRole('button', { name: 'Show in my view' }));

    await waitFor(() => expect(setHiddenMany).toHaveBeenCalledWith(['b'], false));
    expect(toast.success).toHaveBeenCalledWith('Shown in your view again: 1');
  });

  it('says so when the per-user cap refused some files', async () => {
    setHiddenMany.mockResolvedValue([
      { file_uuid: 'a', success: true, message: 'File hidden' },
      { file_uuid: 'c', success: false, message: 'cap', error: 'HIDDEN_LIMIT' },
    ]);
    selectFiles(['a', 'c']);
    await openOrganize([
      { uuid: 'a', status: 'completed' },
      { uuid: 'c', status: 'completed' },
    ]);

    await fireEvent.click(screen.getByRole('button', { name: 'Hide from my view' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'You have hidden the most files allowed. Unhide some files first.'
      )
    );
  });
});
