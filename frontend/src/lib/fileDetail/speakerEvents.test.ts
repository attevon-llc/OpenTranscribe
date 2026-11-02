import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

const mockToast = vi.hoisted(() => ({ info: vi.fn() }));
vi.mock('$stores/toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { listenForSpeakerEvents } from './speakerEvents';

function fire(type: string, detail: Record<string, unknown>) {
  window.dispatchEvent(new CustomEvent(type, { detail }));
}

let stop: () => void;
const ctx = {
  getFileId: () => 'f1',
  getSpeakerList: () => [{ uuid: 'a', name: 'SPEAKER_00', verified: false }],
  loadSpeakers: vi.fn(),
  applySpeakerRename: vi.fn(),
  refetchSegmentsAfterMerge: vi.fn(),
  refreshCaptionTrack: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  stop = listenForSpeakerEvents(ctx);
});
afterEach(() => stop());

describe('speaker-updated', () => {
  it('reloads speakers for this file under either field name, and ignores other files', () => {
    fire('speaker-updated', { file_id: 'f1' });
    fire('speaker-updated', { media_file_id: 'f1' });
    fire('speaker-updated', { file_id: 'f2' });
    expect(ctx.loadSpeakers).toHaveBeenCalledTimes(2);
  });
});

describe('speaker-processing-complete', () => {
  it('repaints a rename with the speaker label, reloads, refreshes captions and toasts', () => {
    fire('speaker-processing-complete', {
      media_file_id: 'f1',
      speaker_uuid: 'a',
      display_name: 'Ann',
      auto_applied_count: 2,
    });
    expect(ctx.applySpeakerRename).toHaveBeenCalledWith('a', 'SPEAKER_00', 'Ann');
    expect(ctx.refetchSegmentsAfterMerge).not.toHaveBeenCalled();
    expect(ctx.loadSpeakers).toHaveBeenCalledTimes(1);
    expect(ctx.refreshCaptionTrack).toHaveBeenCalledWith('speaker processing');
    expect(mockToast.info).toHaveBeenCalledWith('speakerProfile.autoAppliedToOthers');
  });

  it('refetches segments after a merge instead of guessing a rename', () => {
    fire('speaker-processing-complete', {
      media_file_id: 'f1',
      reason: 'speaker_merged',
      speaker_uuid: 'a',
      display_name: 'Ann',
      suggested_count: 1,
    });
    expect(ctx.refetchSegmentsAfterMerge).toHaveBeenCalledTimes(1);
    expect(ctx.applySpeakerRename).not.toHaveBeenCalled();
    expect(mockToast.info).toHaveBeenCalledWith('speakerProfile.suggestionsCreated');
  });

  it('ignores other files, and stops listening when stopped', () => {
    fire('speaker-processing-complete', {
      media_file_id: 'f2',
      speaker_uuid: 'a',
      display_name: 'X',
    });
    stop();
    fire('speaker-updated', { file_id: 'f1' });
    expect(ctx.loadSpeakers).not.toHaveBeenCalled();
    expect(ctx.applySpeakerRename).not.toHaveBeenCalled();
    stop = () => {};
  });
});
