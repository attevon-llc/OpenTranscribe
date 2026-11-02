import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockAxios = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), delete: vi.fn() }));
vi.mock('$lib/axios', () => ({ default: mockAxios }));

const mockToast = vi.hoisted(() => ({ success: vi.fn(), error: vi.fn(), info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));
vi.mock('$stores/transcriptStore', () => ({
  transcriptStore: { loadTranscriptData: vi.fn(), updateSpeakerName: vi.fn() },
}));
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { createFileSpeakers } from './fileSpeakers';
import type { SpeakerItem } from './speakerNames';
import type { SpeakerFile } from './speakerLoader';

function harness(showOriginal = false) {
  const state = {
    file: { uuid: 'f1', transcript_segments: [] } as SpeakerFile,
    list: [] as SpeakerItem[],
  };
  const ctx = {
    getFileId: () => 'f1',
    getFile: () => state.file,
    setFile: vi.fn(),
    getShowOriginal: () => showOriginal,
    getSpeakerList: () => state.list,
    setSpeakerList: (l: SpeakerItem[]) => (state.list = l),
    getBaseline: () => new Map(),
    setBaseline: vi.fn(),
    setSavingSpeakers: vi.fn(),
    setProfileConfirmation: vi.fn(),
    closeSpeakerEditor: vi.fn(),
    isDiarizationDisabled: () => false,
    refreshCaptionTrack: vi.fn(),
    refreshAnalytics: vi.fn(),
    applyReloadedFile: vi.fn(),
    refetchSegmentsAfterMerge: vi.fn(),
  };
  return { state, ctx, speakers: createFileSpeakers(ctx) };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('createFileSpeakers.refreshAfterMerge', () => {
  it('reloads the file honouring show-original, applies it, then reloads speakers', async () => {
    mockAxios.get.mockImplementation((url: string) =>
      Promise.resolve(url === '/speakers' ? { data: [] } : { data: { uuid: 'f1', x: 1 } })
    );
    const { ctx, speakers } = harness(true);

    await speakers.refreshAfterMerge();

    expect(mockAxios.get).toHaveBeenCalledWith('/files/f1', { params: { redact: false } });
    expect(ctx.applyReloadedFile).toHaveBeenCalledWith({ uuid: 'f1', x: 1 });
    expect(mockAxios.get).toHaveBeenLastCalledWith('/speakers', { params: { file_uuid: 'f1' } });
  });

  it('toasts when the refresh fails', async () => {
    mockAxios.get.mockRejectedValue(new Error('down'));
    const { ctx, speakers } = harness();

    await speakers.refreshAfterMerge();

    expect(ctx.applyReloadedFile).not.toHaveBeenCalled();
    expect(mockToast.error).toHaveBeenCalledWith('fileDetail.dataRefreshFailed');
  });
});

describe('createFileSpeakers.listen', () => {
  it('wires the live events to the shared loader and merge refetch', () => {
    mockAxios.get.mockResolvedValue({ data: [] });
    const { ctx, speakers } = harness();
    const stop = speakers.listen();

    window.dispatchEvent(
      new CustomEvent('speaker-processing-complete', {
        detail: { media_file_id: 'f1', reason: 'speaker_merged' },
      })
    );
    stop();

    expect(ctx.refetchSegmentsAfterMerge).toHaveBeenCalledTimes(1);
    expect(mockAxios.get).toHaveBeenCalledWith('/speakers', { params: { file_uuid: 'f1' } });
  });
});
