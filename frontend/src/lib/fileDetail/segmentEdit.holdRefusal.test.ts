/**
 * A segment edit refused because a legal hold was placed after the page loaded: the person
 * sees the translated hold sentence (not "failed to save"), the editor closes, and the page
 * hears who holds the file so it can switch to the frozen view.
 */
import { describe, it, expect, vi } from 'vitest';

const mockAxios = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));
const mockToast = vi.hoisted(() => ({ error: vi.fn(), success: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));
vi.mock('$stores/transcriptStore', () => ({ transcriptStore: { updateSegmentText: vi.fn() } }));
vi.mock('$stores/locale', async () =>
  (await import('../../test-mocks/galleryToolbarMocks')).englishLocale()
);

import { CLOSED_SEGMENT_EDIT, createSegmentEditor, type SegmentEditState } from './segmentEdit';
import { onHoldRefused } from '$lib/legalHold/refusal';
import type { SegmentBearingFile } from './segmentSync';

describe('createSegmentEditor: legal-hold refusal', () => {
  it('shows the hold refusal, closes the editor and reports who holds the file', async () => {
    mockAxios.put.mockRejectedValue({
      response: {
        status: 409,
        data: {
          detail: { error: 'FILE_UNDER_LEGAL_HOLD', message: 'refused', held_by: ['platform'] },
        },
      },
    });
    const state = {
      file: {
        uuid: 'f1',
        transcript_segments: [{ uuid: 's1', text: 'old' }],
      } as SegmentBearingFile,
      edit: CLOSED_SEGMENT_EDIT as SegmentEditState,
    };
    const editor = createSegmentEditor({
      getFileId: () => 'f1',
      getFile: () => state.file,
      setFile: (f: SegmentBearingFile) => (state.file = f),
      getEdit: () => state.edit,
      setEdit: (e: SegmentEditState) => (state.edit = e),
      setSaving: () => {},
      refreshCaptionTrack: vi.fn(),
      refreshAnalytics: vi.fn(),
    });
    const heard: string[][] = [];
    const stop = onHoldRefused((heldBy) => heard.push(heldBy));
    editor.open({ uuid: 's1', text: 'old' });
    state.edit = { ...state.edit, text: 'new' };

    await editor.save({ uuid: 's1' });

    expect(mockToast.error).toHaveBeenCalledTimes(1);
    expect(mockToast.error).toHaveBeenCalledWith(
      expect.stringContaining('legal hold placed by the service operator')
    );
    expect(state.edit).toEqual(CLOSED_SEGMENT_EDIT);
    expect(heard).toEqual([['platform']]);
    stop();
  });
});
