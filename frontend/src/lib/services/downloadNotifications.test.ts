/**
 * `downloadNotifications.ts` bridges `$stores/downloads` (in-memory download-tray
 * state) into the persistent bell (`$stores/websocket`) — issue #569. These tests
 * drive `downloadStore`'s real transitions and assert what the bridge pushes,
 * because the risk here is entirely in the mapping: the wrong message key, a
 * status that doesn't colour the row (N2), a `dismissible: false` that would make
 * a dead client-side download permanently stuck (J14), or an uncapped push list
 * (the bug this issue's currency review found in `addNotification` itself, fixed
 * separately in `websocket.test.ts`).
 *
 * `$stores/websocket` is mocked rather than real: the real module opens an actual
 * WebSocket on import side effects, which is `websocket.test.ts`'s job to drive
 * with `FakeWebSocket`. This file only needs to observe what the bridge CALLS.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const mockWsState = vi.hoisted(() => ({ notifications: [] as Array<Record<string, unknown>> }));
const addNotification = vi.hoisted(() => vi.fn());
const updateNotification = vi.hoisted(() => vi.fn());

vi.mock('$stores/websocket', () => ({
  websocketStore: {
    subscribe: (run: (value: typeof mockWsState) => void) => {
      run(mockWsState);
      return () => {};
    },
    addNotification,
    updateNotification,
  },
}));

// The key stands in for the copy; interpolated values are appended so a
// filename/error substitution is actually observable in the assertion.
vi.mock('$stores/locale', () => ({
  t: {
    subscribe: (run: (value: (key: string, vars?: Record<string, unknown>) => string) => void) => {
      run((key: string, vars?: Record<string, unknown>) =>
        vars ? `${key}:${JSON.stringify(vars)}` : key
      );
      return () => {};
    },
  },
}));

import { downloadStore } from '$stores/downloads';
import { initDownloadNotifications } from './downloadNotifications';

function lastCall() {
  return addNotification.mock.calls[addNotification.mock.calls.length - 1][0];
}

beforeEach(() => {
  vi.clearAllMocks();
  downloadStore.reset();
  mockWsState.notifications = [];
});

describe('boot-time reconcile (J16)', () => {
  it('converts a download row still "processing" from a prior session into a dismissible error, exactly once at init', () => {
    mockWsState.notifications = [
      {
        type: 'download_progress',
        progressId: 'download-stale-file',
        status: 'processing',
        data: { status: 'processing', file_id: 'stale-file' },
      },
    ];

    // This is the module's first `initDownloadNotifications()` call across the
    // whole test file (guarded as a singleton) — it MUST run first so the
    // reconcile-on-init path is actually exercised before the guard makes
    // later calls in other tests no-ops.
    initDownloadNotifications();

    expect(updateNotification).toHaveBeenCalledWith(
      'download-stale-file',
      expect.objectContaining({
        status: 'error',
        message: expect.stringContaining('downloads.downloadFailedError'),
        data: expect.objectContaining({ status: 'error', file_id: 'stale-file' }),
      })
    );
  });

  it('leaves a completed/errored row alone', () => {
    updateNotification.mockClear();
    mockWsState.notifications = [
      { type: 'download_progress', progressId: 'download-done', status: 'completed' },
    ];

    initDownloadNotifications();

    expect(updateNotification).not.toHaveBeenCalled();
  });

  it('is idempotent — a second call does not re-subscribe or double-push', () => {
    downloadStore.startDownload('idempotency-check', 'a.mp4');
    const callsAfterFirstTransition = addNotification.mock.calls.length;

    initDownloadNotifications(); // already initialized by the first test in this file
    downloadStore.updateStatus('idempotency-check', 'downloading', 40);

    // Exactly one more push for the one transition — not two, which a second
    // live subscription would produce.
    expect(addNotification.mock.calls.length).toBe(callsAfterFirstTransition + 1);
  });
});

describe('transition table', () => {
  it('preparing (default video_with_subtitles) → processing, uncoloured claim fixed via data.status', () => {
    downloadStore.startDownload('file-1', 'meeting.mp4');

    const call = lastCall();
    expect(call.type).toBe('download_progress');
    expect(call.title).toBe('notifications.downloadProgress');
    expect(call.message).toBe('downloads.preparingWithSubtitles:{"filename":"meeting.mp4"}');
    expect(call.progressId).toBe('download-file-1');
    expect(call.status).toBe('processing');
    expect(call.dismissible).toBe(true); // J14 — never false, unlike other progressive rows
    expect(call.data).toEqual({ status: 'processing', file_id: 'file-1', filename: 'meeting.mp4' });
  });

  it('preparing (audio) uses the audio-specific copy', () => {
    downloadStore.startDownload('file-audio', 'clip.mp3', 'audio');

    expect(lastCall().message).toBe('downloads.preparingAudio:{"filename":"clip.mp3"}');
  });

  it('preparing (original_video) uses the generic copy', () => {
    downloadStore.startDownload('file-plain', 'raw.mp4', 'original_video');

    expect(lastCall().message).toBe('downloads.preparingDownload:{"filename":"raw.mp4"}');
  });

  it('processing (video_with_subtitles) reports the subtitle-burn-in message', () => {
    downloadStore.startDownload('file-2', 'meeting.mp4');
    downloadStore.updateStatus('file-2', 'processing');

    const call = lastCall();
    expect(call.message).toBe('downloads.addingSubtitles:{"filename":"meeting.mp4"}');
    expect(call.status).toBe('processing');
  });

  it('downloading carries the live percentage and stays status=processing', () => {
    downloadStore.startDownload('file-3', 'meeting.mp4');
    downloadStore.updateStatus('file-3', 'downloading', 55);

    const call = lastCall();
    expect(call.status).toBe('processing');
    expect(call.progress).toEqual({ current: 55, total: 100, percentage: 55 });
  });

  it('completed sets status=completed, percentage=100, and the success message', () => {
    downloadStore.startDownload('file-4', 'meeting.mp4');
    downloadStore.updateStatus('file-4', 'completed');

    const call = lastCall();
    expect(call.status).toBe('completed');
    expect(call.progress).toEqual({ current: 100, total: 100, percentage: 100 });
    expect(call.message).toBe('downloads.downloadedSuccessfully:{"filename":"meeting.mp4"}');
    expect(call.data).toEqual({ status: 'completed', file_id: 'file-4', filename: 'meeting.mp4' });
  });

  it('error carries the real error text, falling back to a translated "unknown error"', () => {
    downloadStore.startDownload('file-5', 'meeting.mp4');
    downloadStore.updateStatus('file-5', 'error', undefined, 'disk full');

    expect(lastCall().message).toBe('downloads.downloadFailedError:{"error":"disk full"}');

    downloadStore.startDownload('file-6', 'other.mp4');
    downloadStore.updateStatus('file-6', 'error');

    expect(lastCall().message).toBe(
      'downloads.downloadFailedError:{"error":"downloads.unknownError"}'
    );
  });
});
