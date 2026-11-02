import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockToast = vi.hoisted(() => ({ error: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { createRedactionView } from './redactionView';
import type { SegmentBearingFile } from './segmentSync';

function harness() {
  const state = {
    file: {
      uuid: 'f1',
      title: 'T',
      transcript_segments: [{ uuid: 'masked' }],
    } as SegmentBearingFile,
    showOriginal: false,
    busy: false,
    busySeen: [] as boolean[],
  };
  const ctx = {
    getFileId: () => 'f1',
    getFile: () => state.file,
    setFile: (f: SegmentBearingFile) => (state.file = f),
    getShowOriginal: () => state.showOriginal,
    setShowOriginal: (v: boolean) => (state.showOriginal = v),
    isToggleBusy: () => state.busy,
    setToggleBusy: (v: boolean) => {
      state.busy = v;
      state.busySeen.push(v);
    },
    applyRedactionState: vi.fn(),
    onTranscriptReloaded: vi.fn(),
  };
  return { state, ctx, view: createRedactionView(ctx) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('createRedactionView.toggleShowOriginal', () => {
  it('flips to the original and swaps only the transcript in place', async () => {
    const data = {
      transcript_segments: [{ uuid: 'plain' }],
      grouped_segments: [],
      title: 'server',
    };
    mockAxios.get.mockResolvedValue({ data });
    const { state, ctx, view } = harness();

    await view.toggleShowOriginal();

    expect(state.showOriginal).toBe(true);
    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1', { params: { redact: false } });
    expect(state.file).toEqual({
      uuid: 'f1',
      title: 'T',
      transcript_segments: [{ uuid: 'plain' }],
      grouped_segments: [],
    });
    expect(ctx.applyRedactionState).toHaveBeenCalledWith(data);
    expect(ctx.onTranscriptReloaded).toHaveBeenCalledTimes(1);
    expect(state.busySeen).toEqual([true, false]);
  });

  it('flips back to the masked read', async () => {
    mockAxios.get.mockResolvedValue({ data: { transcript_segments: [] } });
    const { state, view } = harness();
    state.showOriginal = true;

    await view.toggleShowOriginal();

    expect(state.showOriginal).toBe(false);
    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1', { params: {} });
  });

  it('ignores a second click while one is in flight', async () => {
    const { state, view } = harness();
    state.busy = true;
    await view.toggleShowOriginal();
    expect(state.showOriginal).toBe(false);
    expect(mockAxios.get).not.toHaveBeenCalled();
  });

  it('toasts a failed refresh and clears busy', async () => {
    mockAxios.get.mockRejectedValue(new Error('down'));
    const { state, view } = harness();

    await view.toggleShowOriginal();

    expect(mockToast.error).toHaveBeenCalledWith('fileDetail.failedToLoadFile');
    expect(state.busy).toBe(false);
  });
});
