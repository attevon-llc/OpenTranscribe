/**
 * `handleFileNotification` is the file-detail page's WebSocket dispatch — a state
 * machine with real correctness risk in its branching: LLM-availability gating on
 * spinners, file-id matching before mutating shared state, and a keyword-based
 * classifier that suppresses toasts for "LLM not configured" errors specifically.
 * A wrong branch here shows the wrong spinner or fires an error toast nobody wants.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { handleFileNotification, type FileNotificationContext } from './notificationHandler';
import type { Notification } from '$stores/websocket';

function notification(overrides: Partial<Notification> = {}): Notification {
  return {
    id: 'n1',
    type: 'transcription_status',
    title: '',
    message: '',
    timestamp: new Date(),
    read: false,
    ...overrides,
  } as Notification;
}

function makeContext(overrides: Partial<FileNotificationContext> = {}): {
  ctx: FileNotificationContext;
  file: Record<string, unknown>;
} {
  const file: Record<string, unknown> = { uuid: 'file-1', status: 'processing', progress: 0 };
  const ctx: FileNotificationContext = {
    getFileId: () => 'file-1',
    getFile: () => file,
    getLlmAvailable: () => true,
    getRedactionStatus: () => 'idle',
    getVideoPlayerComponent: () => null,
    setFile: vi.fn((f) => Object.assign(file, f)),
    setCurrentProcessingStep: vi.fn(),
    setSummaryGenerating: vi.fn(),
    setGeneratingSummary: vi.fn(),
    setReprocessing: vi.fn(),
    setRedactionStatus: vi.fn(),
    setRedactionPending: vi.fn(),
    fetchTranscriptData: vi.fn(),
    loadSpeakers: vi.fn(),
    loadAISuggestions: vi.fn(),
    fetchFileDetails: vi.fn(),
    t: (key: string) => key,
    toastError: vi.fn(),
    toastInfo: vi.fn(),
    ...overrides,
  };
  return { ctx, file };
}

describe('transcription_status', () => {
  it('updates progress and the processing step while processing', () => {
    const { ctx, file } = makeContext();

    handleFileNotification(
      notification({
        status: 'processing',
        progress: { current: 5, total: 10, percentage: 42 },
        currentStep: 'Transcribing',
      }),
      ctx
    );

    expect(file.progress).toBe(42);
    expect(file.status).toBe('processing');
    expect(ctx.setCurrentProcessingStep).toHaveBeenCalledWith('Transcribing');
    expect(ctx.setFile).toHaveBeenCalled();
  });

  describe('completion (status/success/complete/finished all alias to the same path)', () => {
    for (const status of ['completed', 'success', 'complete', 'finished']) {
      it(`treats "${status}" as completion`, () => {
        const { ctx, file } = makeContext();

        // `success`/`complete`/`finished` only reach the handler via `data.status`
        // (the top-level field is typed to the canonical 'completed' value only).
        handleFileNotification(notification({ data: { status } }), ctx);

        expect(file.progress).toBe(100);
        expect(file.status).toBe('completed');
      });
    }

    it('shows the AI summary spinner only when the LLM is available', () => {
      const { ctx: withLlm } = makeContext({ getLlmAvailable: () => true });
      handleFileNotification(notification({ status: 'completed' }), withLlm);
      expect(withLlm.setSummaryGenerating).toHaveBeenCalledWith(true);
      expect(withLlm.setReprocessing).not.toHaveBeenCalled();

      const { ctx: withoutLlm } = makeContext({ getLlmAvailable: () => false });
      handleFileNotification(notification({ status: 'completed' }), withoutLlm);
      expect(withoutLlm.setSummaryGenerating).toHaveBeenCalledWith(false);
      expect(withoutLlm.setReprocessing).toHaveBeenCalledWith(false);
    });

    it('refreshes the transcript and subtitles a second after completion, but only once', async () => {
      vi.useFakeTimers();
      try {
        const updateSubtitles = vi.fn().mockResolvedValue(undefined);
        const { ctx } = makeContext({ getVideoPlayerComponent: () => ({ updateSubtitles }) });

        handleFileNotification(notification({ status: 'completed' }), ctx);
        expect(ctx.fetchTranscriptData).not.toHaveBeenCalled();

        await vi.advanceTimersByTimeAsync(1000);

        expect(ctx.setCurrentProcessingStep).toHaveBeenLastCalledWith('');
        expect(ctx.fetchTranscriptData).toHaveBeenCalledTimes(1);
        expect(updateSubtitles).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });

    it('does not blow up when the video player subtitle refresh rejects', async () => {
      vi.useFakeTimers();
      try {
        const updateSubtitles = vi.fn().mockRejectedValue(new Error('boom'));
        const { ctx } = makeContext({ getVideoPlayerComponent: () => ({ updateSubtitles }) });

        handleFileNotification(notification({ status: 'completed' }), ctx);
        await vi.advanceTimersByTimeAsync(1000);

        expect(ctx.fetchTranscriptData).toHaveBeenCalledTimes(1);
      } finally {
        vi.useRealTimers();
      }
    });
  });

  it('refreshes file details immediately on error/failed, with no delay', () => {
    const { ctx } = makeContext();

    handleFileNotification(notification({ status: 'error' }), ctx);

    expect(ctx.setCurrentProcessingStep).toHaveBeenCalledWith('');
    expect(ctx.fetchFileDetails).toHaveBeenCalledTimes(1);
  });
});

describe('summarization_status', () => {
  it('ignores a notification for a different file entirely', () => {
    const { ctx } = makeContext();

    handleFileNotification(
      notification({
        type: 'summarization_status',
        data: { file_id: 'some-other-file', status: 'completed' },
      }),
      ctx
    );

    expect(ctx.setSummaryGenerating).not.toHaveBeenCalled();
    expect(ctx.setFile).not.toHaveBeenCalled();
  });

  it('sets has_summary only when the notification actually carries a preview', () => {
    const { ctx, file } = makeContext();

    handleFileNotification(
      notification({
        type: 'summarization_status',
        data: { file_id: 'file-1', status: 'completed', summary: 'a short preview' },
      }),
      ctx
    );

    expect(file.has_summary).toBe(true);
    expect(ctx.setReprocessing).toHaveBeenCalledWith(false);
  });

  it('does NOT toast an error when the failure is an LLM-not-configured message', () => {
    const { ctx } = makeContext();

    handleFileNotification(
      notification({
        type: 'summarization_status',
        data: {
          file_id: 'file-1',
          status: 'failed',
          message: 'Please configure an LLM provider first',
        },
      }),
      ctx
    );

    expect(ctx.toastError).not.toHaveBeenCalled();
    expect(ctx.setSummaryGenerating).toHaveBeenCalledWith(false);
  });

  it('DOES toast an error for a genuine failure', () => {
    const { ctx } = makeContext();

    handleFileNotification(
      notification({
        type: 'summarization_status',
        data: { file_id: 'file-1', status: 'failed', message: 'Backend crashed' },
      }),
      ctx
    );

    expect(ctx.toastError).toHaveBeenCalledWith('Backend crashed', 5000);
  });
});

describe('topic_extraction_status', () => {
  it('reloads AI suggestions on completion and clears the step after a delay', async () => {
    vi.useFakeTimers();
    try {
      const { ctx } = makeContext();

      handleFileNotification(
        notification({ type: 'topic_extraction_status', status: 'completed' }),
        ctx
      );

      expect(ctx.loadAISuggestions).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(2000);
      expect(ctx.setCurrentProcessingStep).toHaveBeenLastCalledWith('');
    } finally {
      vi.useRealTimers();
    }
  });

  it('logs rather than throws when reloading suggestions rejects', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { ctx } = makeContext({ loadAISuggestions: vi.fn().mockRejectedValue(new Error('x')) });

    expect(() =>
      handleFileNotification(
        notification({ type: 'topic_extraction_status', status: 'completed' }),
        ctx
      )
    ).not.toThrow();
    await Promise.resolve(); // let the rejected promise's .catch() run

    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('Error reloading AI suggestions'),
      expect.any(Error)
    );
    consoleError.mockRestore();
  });
});

describe('redaction_status', () => {
  it('on "done": clears pending, refreshes the file, and surfaces skipped detectors', () => {
    const { ctx } = makeContext();

    handleFileNotification(
      notification({
        type: 'redaction_status',
        data: { status: 'done', skipped_detectors: ['toxicity'] },
      }),
      ctx
    );

    expect(ctx.setRedactionPending).toHaveBeenCalledWith(false);
    expect(ctx.fetchFileDetails).toHaveBeenCalledTimes(1);
    expect(ctx.toastInfo).toHaveBeenCalled();
  });

  it('on an in-progress status: marks pending true without refreshing', () => {
    const { ctx } = makeContext();

    handleFileNotification(notification({ type: 'redaction_status', status: 'processing' }), ctx);

    expect(ctx.setRedactionPending).toHaveBeenCalledWith(true);
    expect(ctx.fetchFileDetails).not.toHaveBeenCalled();
  });
});

describe('cache_invalidate', () => {
  it('refreshes only when scope is "files" AND the file id matches', () => {
    const { ctx } = makeContext();

    handleFileNotification(
      notification({ type: 'cache_invalidate', data: { scope: 'other', file_id: 'file-1' } }),
      ctx
    );
    expect(ctx.fetchFileDetails).not.toHaveBeenCalled();

    handleFileNotification(
      notification({ type: 'cache_invalidate', data: { scope: 'files', file_id: 'file-1' } }),
      ctx
    );
    expect(ctx.fetchFileDetails).toHaveBeenCalledTimes(1);
    expect(ctx.loadAISuggestions).toHaveBeenCalledTimes(1);
  });

  // BC-13 regression: this call site previously invoked ctx.loadAISuggestions()
  // bare, with no .catch — an unhandled rejection risk, inconsistent with the
  // topic_extraction_status branch's already-caught call to the same callback.
  it('logs rather than throws when reloading suggestions rejects', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { ctx } = makeContext({ loadAISuggestions: vi.fn().mockRejectedValue(new Error('x')) });

    expect(() =>
      handleFileNotification(
        notification({ type: 'cache_invalidate', data: { scope: 'files', file_id: 'file-1' } }),
        ctx
      )
    ).not.toThrow();
    await Promise.resolve(); // let the rejected promise's .catch() run

    expect(consoleError).toHaveBeenCalledWith(
      expect.stringContaining('Error reloading AI suggestions'),
      expect.any(Error)
    );
    consoleError.mockRestore();
  });
});
