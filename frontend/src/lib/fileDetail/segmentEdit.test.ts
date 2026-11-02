import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockToast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));
const mockStore = vi.hoisted(() => ({ updateSegmentText: vi.fn() }));
vi.mock('$stores/transcriptStore', () => ({ transcriptStore: mockStore }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import {
  CLOSED_SEGMENT_EDIT,
  clearProcessedVideoCache,
  createSegmentEditor,
  hasUnsavedSegmentEdit,
  saveSegmentText,
  segmentSaveErrorKey,
  type SegmentEditState,
} from './segmentEdit';
import type { SegmentBearingFile } from './segmentSync';

beforeEach(() => vi.clearAllMocks());

describe('saveSegmentText', () => {
  it('PUTs only the text and resolves the stored row', async () => {
    mockAxios.put.mockResolvedValue({ data: { text: 'stored' } });

    expect(await saveSegmentText('f1', 's1', 'typed')).toEqual({ text: 'stored' });
    expect(mockAxios.put).toHaveBeenCalledWith('/files/f1/transcript/segments/s1', {
      text: 'typed',
    });
  });

  it('resolves null for an empty response and rejects on failure', async () => {
    mockAxios.put.mockResolvedValueOnce({ data: '' });
    expect(await saveSegmentText('f1', 's1', 'x')).toBeNull();
    mockAxios.put.mockRejectedValueOnce({ response: { status: 422 } });
    await expect(saveSegmentText('f1', 's1', 'x')).rejects.toEqual({ response: { status: 422 } });
  });
});

describe('segmentSaveErrorKey', () => {
  it.each([
    [405, 'fileDetail.transcriptEditingNotSupported'],
    [404, 'fileDetail.transcriptSegmentNotFound'],
    [422, 'fileDetail.invalidSegmentData'],
    [500, 'fileDetail.failedToSaveSegment'],
    [undefined, 'fileDetail.failedToSaveSegment'],
  ])('status %s -> %s', (status, key) => {
    expect(segmentSaveErrorKey(status)).toBe(key);
  });
});

describe('clearProcessedVideoCache', () => {
  it('deletes the cache and swallows a failure with a warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    mockAxios.delete.mockRejectedValue(new Error('x'));

    clearProcessedVideoCache('f1');
    await Promise.resolve();
    await Promise.resolve();

    expect(mockAxios.delete).toHaveBeenCalledWith('/files/f1/cache');
    expect(warn).toHaveBeenCalled();
    warn.mockRestore();
  });
});

describe('hasUnsavedSegmentEdit', () => {
  it('only an open editor whose trimmed text moved is unsaved', () => {
    expect(hasUnsavedSegmentEdit(CLOSED_SEGMENT_EDIT)).toBe(false);
    expect(hasUnsavedSegmentEdit({ id: 's1', text: 'hi ', originalText: 'hi' })).toBe(false);
    expect(hasUnsavedSegmentEdit({ id: 's1', text: 'hello', originalText: 'hi' })).toBe(true);
  });
});

describe('createSegmentEditor', () => {
  function harness() {
    const state = {
      file: {
        uuid: 'f1',
        transcript_segments: [{ uuid: 's1', text: 'old', speaker_label: 'SPEAKER_00' }],
      } as SegmentBearingFile,
      edit: CLOSED_SEGMENT_EDIT as SegmentEditState,
      saving: [] as boolean[],
    };
    const ctx = {
      getFileId: () => 'f1',
      getFile: () => state.file,
      setFile: (f: SegmentBearingFile) => (state.file = f),
      getEdit: () => state.edit,
      setEdit: (e: SegmentEditState) => (state.edit = e),
      setSaving: (v: boolean) => state.saving.push(v),
      refreshCaptionTrack: vi.fn(),
      refreshAnalytics: vi.fn(),
    };
    return { state, ctx, editor: createSegmentEditor(ctx) };
  }

  it('opens with the segment text as the baseline, and cancel closes', () => {
    const { state, editor } = harness();
    editor.open({ uuid: 's1', text: 'old' });
    expect(state.edit).toEqual({ id: 's1', text: 'old', originalText: 'old' });
    editor.cancel();
    expect(state.edit).toEqual(CLOSED_SEGMENT_EDIT);
  });

  it('saves the text, patches only it, closes, and refreshes what derives from it', async () => {
    mockAxios.put.mockResolvedValue({ data: { text: 'new (stored)' } });
    mockAxios.delete.mockResolvedValue({});
    const { state, ctx, editor } = harness();
    editor.open({ uuid: 's1', text: 'old' });
    state.edit = { ...state.edit, text: 'new' };

    await editor.save({ uuid: 's1' });

    expect(mockAxios.put).toHaveBeenCalledWith('/files/f1/transcript/segments/s1', { text: 'new' });
    expect(mockStore.updateSegmentText).toHaveBeenCalledWith('s1', 'new');
    expect(state.file.transcript_segments?.[0]).toEqual({
      uuid: 's1',
      text: 'new (stored)',
      speaker_label: 'SPEAKER_00',
    });
    expect(state.edit).toEqual(CLOSED_SEGMENT_EDIT);
    expect(mockAxios.delete).toHaveBeenCalledWith('/files/f1/cache');
    expect(ctx.refreshCaptionTrack).toHaveBeenCalledWith('segment edit');
    expect(ctx.refreshAnalytics).toHaveBeenCalled();
    expect(state.saving).toEqual([true, false]);
  });

  it('keeps the editor open and toasts the reason when the save fails', async () => {
    mockAxios.put.mockRejectedValue({ response: { status: 405 } });
    vi.spyOn(console, 'error').mockImplementation(() => {});
    const { state, editor } = harness();
    editor.open({ uuid: 's1', text: 'old' });
    state.edit = { ...state.edit, text: 'new' };

    await editor.save({ uuid: 's1' });

    expect(state.edit.id).toBe('s1');
    expect(mockToast.error).toHaveBeenCalledWith('fileDetail.transcriptEditingNotSupported');
    expect(state.saving).toEqual([true, false]);
  });

  it('never saves an empty text', async () => {
    const { state, editor } = harness();
    editor.open({ uuid: 's1', text: 'old' });
    state.edit = { ...state.edit, text: '' };
    await editor.save({ uuid: 's1' });
    expect(mockAxios.put).not.toHaveBeenCalled();
    expect(state.edit.id).toBe('s1');
    expect(state.saving).toEqual([]);
  });
});
