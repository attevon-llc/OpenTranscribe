/**
 * The gallery's delete (issue #1101): the confirmation names the server's trash window, the
 * bulk results are sorted into trashed / forbidden / held / other, and Undo restores once.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const axiosMock = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: axiosMock }));

import { get } from 'svelte/store';
import { createUndo, summarizeTrashResults, trashConfirmMessage } from './galleryTrash';

const { englishLocale } = await import('../../test-mocks/galleryToolbarMocks');
const t = get(englishLocale().t);

beforeEach(() => {
  vi.clearAllMocks();
});

describe('trashConfirmMessage', () => {
  it('says the files are recoverable for the server window, read from the trash list', async () => {
    axiosMock.get.mockResolvedValue({
      data: { items: [], total: 0, page: 1, page_size: 1, retention_days: 7 },
    });
    const text = await trashConfirmMessage(t);
    expect(axiosMock.get).toHaveBeenCalledWith('/files/trash', {
      params: { page: 1, page_size: 1 },
    });
    expect(text).toBe(
      'The selected files move to the Trash. Recoverable for 7 days, then deleted forever.'
    );
  });

  it('uses the singular for a one-day window', async () => {
    axiosMock.get.mockResolvedValue({
      data: { items: [], total: 0, page: 1, page_size: 1, retention_days: 1 },
    });
    expect(await trashConfirmMessage(t)).toContain('Recoverable for 1 day,');
  });

  it('still says the files are recoverable when the window cannot be read', async () => {
    axiosMock.get.mockRejectedValue(new Error('offline'));
    const text = await trashConfirmMessage(t);
    expect(text).toMatch(/restore them until they are deleted forever/);
    expect(text).not.toMatch(/cannot be undone/i);
  });
});

describe('summarizeTrashResults', () => {
  it('sorts each file into exactly one outcome', () => {
    expect(
      summarizeTrashResults([
        { file_uuid: 'a', success: true, message: '' },
        { file_uuid: 'b', success: false, message: '', error: 'FORBIDDEN' },
        { file_uuid: 'c', success: false, message: '', error: 'FILE_UNDER_LEGAL_HOLD' },
        { file_uuid: 'd', success: false, message: '', error: 'NOT_FOUND' },
        { file_uuid: 'e', success: true, message: '' },
      ])
    ).toEqual({ trashed: ['a', 'e'], forbidden: 1, held: 1, other: 1 });
  });
});

describe('createUndo', () => {
  it('restores the trashed files through the bulk restore action, once', async () => {
    axiosMock.post.mockResolvedValue({
      data: [
        { file_uuid: 'a', success: true, message: '' },
        { file_uuid: 'b', success: true, message: '' },
      ],
    });
    const undo = createUndo(['a', 'b']);
    expect(await undo()).toBe(true);
    expect(axiosMock.post).toHaveBeenCalledWith('/files/management/bulk-action', {
      file_uuids: ['a', 'b'],
      action: 'restore',
    });
    expect(await undo()).toBeNull();
    expect(axiosMock.post).toHaveBeenCalledTimes(1);
  });

  it('reports a partial or failed restore as false', async () => {
    axiosMock.post.mockResolvedValueOnce({
      data: [
        { file_uuid: 'a', success: true, message: '' },
        { file_uuid: 'b', success: false, message: '', error: 'NOT_RESTORABLE' },
      ],
    });
    expect(await createUndo(['a', 'b'])()).toBe(false);
    axiosMock.post.mockRejectedValueOnce(new Error('down'));
    expect(await createUndo(['a'])()).toBe(false);
  });
});
