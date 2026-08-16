import type { Notification } from '$stores/websocket';

/**
 * Translation function shape — matches the value of the `t` derived store
 * (`$t` inside the page): `(key, options?) => string`.
 */
type TranslateFn = (key: string, options?: Record<string, unknown>) => string;

/**
 * Context object the file-detail page passes into {@link handleFileNotification}.
 *
 * The page keeps owning all state. This module is pure logic that reads/writes
 * page state exclusively through these accessors/setters/callbacks, so that the
 * Svelte reactivity and downstream `$:` blocks fire exactly as they did when the
 * switch lived inline in `onMount`.
 *
 * CRITICAL: `setFile` must perform the real `file = ...` assignment in the page
 * (not a no-op clone) so reactivity fires — that assignment is the only thing
 * that propagates an update to the child components.
 */
export interface FileNotificationContext {
  // --- current-file identity & state reads ---
  getFileId: () => string;
  getFile: () => any;
  getLlmAvailable: () => boolean;
  getRedactionStatus: () => string;
  getVideoPlayerComponent: () => any;

  // --- file object mutation (must do the real `file = ...` assignment) ---
  setFile: (f: any) => void;

  // --- flag setters ---
  setCurrentProcessingStep: (s: string) => void;
  setSummaryGenerating: (v: boolean) => void;
  setGeneratingSummary: (v: boolean) => void;
  setReprocessing: (v: boolean) => void;
  setRedactionStatus: (s: string) => void;
  setRedactionPending: (v: boolean) => void;

  // --- page callbacks ---
  fetchTranscriptData: () => void | Promise<void>;
  loadSpeakers: () => void | Promise<void>;
  loadAISuggestions: () => void | Promise<void>;
  fetchFileDetails: () => void | Promise<void>;

  // --- side-effect helpers ---
  t: TranslateFn;
  toastError: (message: string, durationMs?: number) => void;
  toastInfo: (message: string) => void;
}

/**
 * Handle a single (latest, deduplicated) WebSocket notification for the current
 * file. Behavior is intentionally identical to the original inline switch in
 * `files/[id]/+page.svelte`: same mutations, same order, same conditions, same
 * function calls.
 *
 * Callers are responsible for the upstream filtering/dedup/file-id gate exactly
 * as before; this function assumes `latestNotification` already passed the
 * `notificationFileId === currentFileId` outer check.
 */
export function handleFileNotification(
  latestNotification: Notification,
  ctx: FileNotificationContext
): void {
  const fileId = ctx.getFileId();
  const { t } = ctx;

  // Handle transcription status updates
  if (latestNotification.type === 'transcription_status') {
    // Get status from notification (progressive notifications set it at root level)
    const notificationStatus = latestNotification.status || latestNotification.data?.status;
    const notificationProgress =
      latestNotification.progress?.percentage || latestNotification.data?.progress;

    // Update progress in real-time for processing updates
    if (notificationStatus === 'processing' && notificationProgress !== undefined) {
      const file = ctx.getFile();
      if (file) {
        file.progress = notificationProgress;
        file.status = 'processing';
        // Update the current processing step from the progressive notification
        ctx.setCurrentProcessingStep(
          latestNotification.currentStep ||
            latestNotification.message ||
            latestNotification.data?.message ||
            t('fileDetail.processingDefault')
        );
        ctx.setFile({ ...file }); // Trigger reactivity
      }
    } else if (
      notificationStatus === 'completed' ||
      notificationStatus === 'success' ||
      notificationStatus === 'complete' ||
      notificationStatus === 'finished'
    ) {
      // Transcription completed - show completion and refresh
      const file = ctx.getFile();
      if (file) {
        file.progress = 100;
        file.status = 'completed';
        ctx.setCurrentProcessingStep(t('fileDetail.processingComplete'));

        // Show AI summary spinner only if LLM is available after transcription completion
        if (ctx.getLlmAvailable()) {
          ctx.setSummaryGenerating(true);
          ctx.setGeneratingSummary(true);
          // Keep reprocessing flag true until summary completes to maintain proper UI state
        } else {
          // No LLM available, ensure spinners are off and reset reprocessing flag
          ctx.setSummaryGenerating(false);
          ctx.setGeneratingSummary(false);
          ctx.setReprocessing(false);
        }

        ctx.setFile({ ...file }); // Trigger reactivity
      }

      // Clear processing step and refresh transcript data after completion
      setTimeout(async () => {
        ctx.setCurrentProcessingStep(''); // Clear processing step

        // Only refresh the transcript data, not the entire file object to preserve spinner state
        const file = ctx.getFile();
        if (file?.uuid && (file.status === 'completed' || file.status === 'success')) {
          await ctx.fetchTranscriptData();

          // Refresh subtitles in the video player now that transcript is available
          const videoPlayerComponent = ctx.getVideoPlayerComponent();
          if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
            try {
              await videoPlayerComponent.updateSubtitles();
            } catch (error) {
              console.warn('Failed to update subtitles:', error);
            }
          }
        }
      }, 1000);
    } else if (notificationStatus === 'error' || notificationStatus === 'failed') {
      // Error state - refresh immediately
      ctx.setCurrentProcessingStep(''); // Clear processing step
      ctx.fetchFileDetails();
    }
  }

  // WebSocket notifications for file updates

  // Handle summarization status updates
  if (latestNotification.type === 'summarization_status') {
    // Only process notifications for the current file
    const notificationFileId = String(latestNotification.data?.file_id || '');
    const currentFileId = String(fileId || '');

    if (notificationFileId !== currentFileId) {
      // Skip notifications for other files
    } else {
      // Get status from notification (progressive notifications set it at root level)
      const status = latestNotification.status || latestNotification.data?.status;

      if (status === 'queued' || status === 'processing' || status === 'generating') {
        // Summary generation started - show spinner only if LLM is available
        if (ctx.getLlmAvailable()) {
          ctx.setSummaryGenerating(true);
          ctx.setGeneratingSummary(true);
        } else {
          // LLM not available, ensure spinners are off
          ctx.setSummaryGenerating(false);
          ctx.setGeneratingSummary(false);
        }
      } else if (
        status === 'completed' ||
        status === 'success' ||
        status === 'complete' ||
        status === 'finished'
      ) {
        // Summary completed - stop spinners and update file
        ctx.setSummaryGenerating(false);
        ctx.setGeneratingSummary(false);

        // Reset reprocessing flag when summary completes (final step of reprocessing)
        ctx.setReprocessing(false);

        const file = ctx.getFile();
        if (file) {
          // Update summary-related fields from notification data
          // Note: The notification contains a brief preview, not the full summary_data.
          // It no longer carries `summary_opensearch_id` — the transcript_summaries
          // index is retired (#67) — and the preview is always sent on completion,
          // so it alone establishes that a summary now exists.
          const summaryPreview = latestNotification.data?.summary;

          // Set a flag to indicate summary exists (full data fetched via API)
          if (summaryPreview) {
            file.has_summary = true; // Summary now available
          }

          // Force reactivity update by creating new object reference
          ctx.setFile({ ...file });
        }
      } else if (status === 'failed' || status === 'error') {
        // Summary failed - stop spinners and show error
        ctx.setSummaryGenerating(false);
        ctx.setGeneratingSummary(false);

        // Get error message from notification
        const errorMessage =
          latestNotification.data?.message ||
          latestNotification.message ||
          t('fileDetail.failedToGenerateSummaryGeneric');
        const isLLMConfigError =
          errorMessage.toLowerCase().includes('llm service is not available') ||
          errorMessage.toLowerCase().includes('configure an llm provider') ||
          errorMessage.toLowerCase().includes('llm provider');

        if (!isLLMConfigError) {
          ctx.toastError(errorMessage, 5000);
        }
      }
    } // Close the else block for file ID matching
  }

  // `speaker_updated` and `speaker_processing_complete` are handled by window listeners in
  // the page, not here: `stores/websocket.ts` dispatches them as CustomEvents and returns
  // before the notification reaches the store, so branches for them in this function can
  // never run. Two of them lived here and silently did nothing.

  // Handle topic extraction status updates (AI suggestions for tags/collections)
  if (latestNotification.type === 'topic_extraction_status') {
    const status = latestNotification.status || latestNotification.data?.status;
    const message = latestNotification.message || latestNotification.data?.message;

    if (status === 'processing') {
      // Update processing step to show what's happening
      ctx.setCurrentProcessingStep(message || t('fileDetail.analyzingTranscript'));
    } else if (status === 'completed') {
      // Show completion message briefly before clearing
      ctx.setCurrentProcessingStep(message || t('fileDetail.aiSuggestionsComplete'));

      // Reload AI suggestions when extraction completes
      // This will fetch the newly generated suggestions and update the UI dynamically
      Promise.resolve(ctx.loadAISuggestions()).catch((err) => {
        console.error('Error reloading AI suggestions after extraction:', err);
      });

      // Clear processing step after a brief delay
      setTimeout(() => {
        ctx.setCurrentProcessingStep('');
      }, 2000);
    } else if (status === 'failed' || status === 'not_configured') {
      // Clear processing step on failure
      ctx.setCurrentProcessingStep('');
    }
  }

  // Handle content-redaction status (chip + auto-refresh the transcript)
  if (latestNotification.type === 'redaction_status') {
    const rstatus = latestNotification.status || latestNotification.data?.status;
    ctx.setRedactionStatus(rstatus || ctx.getRedactionStatus());
    if (rstatus === 'done') {
      // Detection finished — load the now-available (masked) transcript.
      ctx.setRedactionPending(false);
      ctx.fetchFileDetails();
      const skipped = latestNotification.data?.skipped_detectors || [];
      if (skipped.length) {
        ctx.toastInfo(
          t('settings.contentRedaction.someDetectorsSkipped', {
            detectors: skipped.join(', '),
          })
        );
      }
    } else if (rstatus === 'failed') {
      ctx.setRedactionPending(false);
      ctx.fetchFileDetails();
    } else {
      ctx.setRedactionPending(true);
    }
  }

  // Handle cache invalidation (e.g., auto-label applied tags/collections)
  if (
    latestNotification.type === 'cache_invalidate' &&
    latestNotification.data?.scope === 'files' &&
    latestNotification.data?.file_id === fileId
  ) {
    ctx.fetchFileDetails();
    ctx.loadAISuggestions();
  }
}
