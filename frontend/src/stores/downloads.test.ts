/**
 * `downloadStore` tracks in-flight download jobs and drives toasts off status
 * transitions. These tests focus on the desync risks: starting a second download
 * for a file that's already in flight, and the read-only accessors — which, until
 * fixed here, went through `update()` (firing every subscriber on every read,
 * including when nothing changed) and could return `undefined` instead of
 * `false`/`null` for an untracked file id.
 *
 * This store used to also push a persistent notification into `$stores/notifications`
 * on every transition, but that store's list was rendered by nothing —
 * `NotificationsPanel.svelte` renders exclusively from `$websocketStore.notifications`
 * — so those dead writes were deleted (issue G9). Downloads already surface their own
 * toasts (asserted below) and inline progress via `TranscriptDisplay.svelte`.
 */
import { describe, it, expect, vi, beforeEach, afterEach, type Mock } from 'vitest';
import { get } from 'svelte/store';

const mockToast = vi.hoisted(() => ({ success: vi.fn(), warning: vi.fn(), error: vi.fn() }));
vi.mock('./toast', () => ({ toastStore: mockToast }));

vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string) => key);
      return () => {};
    },
  },
}));

import { downloadStore } from './downloads';

beforeEach(() => {
  vi.clearAllMocks();
  downloadStore.reset();
});

describe('startDownload', () => {
  it('starts a fresh download', () => {
    const started = downloadStore.startDownload('file-1', 'meeting.mp4');

    expect(started).toBe(true);
    expect(get(downloadStore)['file-1']).toMatchObject({
      status: 'preparing',
      downloadType: 'video_with_subtitles',
    });
  });

  it('refuses to start a second download while one is already in flight', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');

    const startedAgain = downloadStore.startDownload('file-1', 'meeting.mp4');

    expect(startedAgain).toBe(false);
    expect(mockToast.warning).toHaveBeenCalled();
  });

  it('allows starting again once the previous download finished', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');
    downloadStore.updateStatus('file-1', 'completed');

    const startedAgain = downloadStore.startDownload('file-1', 'meeting.mp4');

    expect(startedAgain).toBe(true);
  });
});

describe('updateStatus', () => {
  it('is a no-op for a file that was never started', () => {
    expect(() => downloadStore.updateStatus('never-started', 'processing')).not.toThrow();
    expect(get(downloadStore)['never-started']).toBeUndefined();
  });

  it('carries progress and error through to the stored state', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');
    downloadStore.updateStatus('file-1', 'downloading', 42);

    expect(get(downloadStore)['file-1']).toMatchObject({ status: 'downloading', progress: 42 });
  });

  it('is a silent transition (no toast) for "processing" and "downloading"', () => {
    // Neither status has a toast or notification side effect — only the stored
    // state changes; UI feedback for these phases is the inline progress bar.
    downloadStore.startDownload('file-1', 'meeting.mp4');

    downloadStore.updateStatus('file-1', 'processing');
    expect(get(downloadStore)['file-1']).toMatchObject({ status: 'processing' });

    downloadStore.updateStatus('file-1', 'downloading');
    expect(get(downloadStore)['file-1']).toMatchObject({ status: 'downloading' });

    expect(mockToast.success).not.toHaveBeenCalled();
    expect(mockToast.error).not.toHaveBeenCalled();
  });

  describe('completed', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('shows a success toast and removes the entry after the keep-alive window', () => {
      downloadStore.startDownload('file-1', 'meeting.mp4');

      downloadStore.updateStatus('file-1', 'completed');

      expect(mockToast.success).toHaveBeenCalled();
      expect(get(downloadStore)['file-1']).toBeDefined();

      vi.advanceTimersByTime(30000);
      expect(get(downloadStore)['file-1']).toBeUndefined();
    });
  });

  describe('error', () => {
    beforeEach(() => vi.useFakeTimers());
    afterEach(() => vi.useRealTimers());

    it('toasts and removes the entry after the longer error keep-alive window', () => {
      downloadStore.startDownload('file-1', 'meeting.mp4');

      downloadStore.updateStatus('file-1', 'error', undefined, 'disk full');

      expect(mockToast.error).toHaveBeenCalled();

      vi.advanceTimersByTime(59999);
      expect(get(downloadStore)['file-1']).toBeDefined();
      vi.advanceTimersByTime(1);
      expect(get(downloadStore)['file-1']).toBeUndefined();
    });
  });
});

describe('removeDownload', () => {
  it('deletes only the named entry', () => {
    downloadStore.startDownload('file-1', 'a.mp4');
    downloadStore.startDownload('file-2', 'b.mp4');

    downloadStore.removeDownload('file-1');

    expect(get(downloadStore)['file-1']).toBeUndefined();
    expect(get(downloadStore)['file-2']).toBeDefined();
  });
});

describe('isDownloading / getDownloadStatus — read-only accessors', () => {
  it('returns false/null (not undefined) for a file that has no entry', () => {
    expect(downloadStore.isDownloading('missing')).toBe(false);
    expect(downloadStore.getDownloadStatus('missing')).toBeNull();
  });

  it('reflects the true in-flight state for a tracked file', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');
    expect(downloadStore.isDownloading('file-1')).toBe(true);

    downloadStore.updateStatus('file-1', 'completed');
    expect(downloadStore.isDownloading('file-1')).toBe(false);
    expect(downloadStore.getDownloadStatus('file-1')?.status).toBe('completed');
  });

  it('does not notify subscribers on a read — a pure query must not be an update', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');
    const subscriber: Mock = vi.fn();
    const unsubscribe = downloadStore.subscribe(subscriber);
    subscriber.mockClear(); // drop the immediate subscribe-time call

    downloadStore.isDownloading('file-1');
    downloadStore.getDownloadStatus('file-1');

    expect(subscriber).not.toHaveBeenCalled();
    unsubscribe();
  });
});

describe('reset', () => {
  it('clears every tracked download', () => {
    downloadStore.startDownload('file-1', 'a.mp4');
    downloadStore.startDownload('file-2', 'b.mp4');

    downloadStore.reset();

    expect(get(downloadStore)).toEqual({});
  });
});
