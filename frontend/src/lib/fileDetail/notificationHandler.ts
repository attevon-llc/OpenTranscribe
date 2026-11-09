import type { Translate } from '$lib/i18n';
import type { Notification } from '$stores/websocket';

/** The slice of the page's file object that notifications read and write; the rest passes through. */
interface NotificationFile {
  uuid?: string;
  status?: string;
  progress?: unknown;
  has_summary?: boolean;
  [key: string]: unknown;
}

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
  getFile: () => NotificationFile | null;
  getLlmAvailable: () => boolean;
  getRedactionStatus: () => string;
  getVideoPlayerComponent: () => { updateSubtitles?: () => Promise<unknown> } | null;

  // --- file object mutation (must do the real `file = ...` assignment) ---
  setFile: (f: NotificationFile) => void;

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
  t: Translate;
  toastError: (message: string, durationMs?: number) => void;
  toastInfo: (message: string) => void;
}

type Ctx = FileNotificationContext;

const COMPLETED_STATUSES = new Set(['completed', 'success', 'complete', 'finished']);

// Progressive notifications set the status at the root level; others nest it under `data`.
function statusOf(n: Notification): string | undefined {
  return n.status || n.data?.status;
}

// The transcript is committed and readable before the job finishes, so show it now instead
// of at completion. Progress deliberately keeps running — speaker matching, indexing and
// enrichment still follow, and speaker labels attach in place via `speaker_updated` below.
function onTranscriptReady(ctx: Ctx): void {
  if (ctx.getFile()?.uuid) {
    ctx.fetchTranscriptData();
  }
}

// Update progress in real-time for processing updates
function onTranscriptionProgress(n: Notification, progress: unknown, ctx: Ctx): void {
  const file = ctx.getFile();
  if (!file) return;
  file.progress = progress;
  file.status = 'processing';
  // Update the current processing step from the progressive notification
  ctx.setCurrentProcessingStep(
    n.currentStep || n.message || n.data?.message || ctx.t('fileDetail.processingDefault')
  );
  ctx.setFile({ ...file }); // Trigger reactivity
}

// Refresh the transcript (not the whole file object, to preserve spinner state) and the
// video player's subtitles once the transcript is available.
async function refreshAfterTranscription(ctx: Ctx): Promise<void> {
  ctx.setCurrentProcessingStep(''); // Clear processing step
  const file = ctx.getFile();
  if (!file?.uuid || file.status !== 'completed') return;
  await ctx.fetchTranscriptData();

  const videoPlayerComponent = ctx.getVideoPlayerComponent();
  if (videoPlayerComponent && videoPlayerComponent.updateSubtitles) {
    try {
      await videoPlayerComponent.updateSubtitles();
    } catch (error) {
      console.warn('Failed to update subtitles:', error);
    }
  }
}

// Show the AI summary spinner only if an LLM is available after transcription completes.
function setSummarySpinners(ctx: Ctx): void {
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
}

// Transcription completed - show completion and refresh
function onTranscriptionComplete(ctx: Ctx): void {
  const file = ctx.getFile();
  if (file) {
    file.progress = 100;
    file.status = 'completed';
    ctx.setCurrentProcessingStep(ctx.t('fileDetail.processingComplete'));
    setSummarySpinners(ctx);
    ctx.setFile({ ...file }); // Trigger reactivity
  }
  setTimeout(() => refreshAfterTranscription(ctx), 1000);
}

function onTranscription(n: Notification, ctx: Ctx): void {
  const status = statusOf(n);
  const progress = n.progress?.percentage || n.data?.progress;
  if (status === 'processing' && progress !== undefined) {
    onTranscriptionProgress(n, progress, ctx);
  } else if (status !== undefined && COMPLETED_STATUSES.has(status)) {
    onTranscriptionComplete(ctx);
  } else if (status === 'error' || status === 'failed') {
    // Error state - refresh immediately
    ctx.setCurrentProcessingStep(''); // Clear processing step
    ctx.fetchFileDetails();
  }
}

// An LLM-not-configured failure is a setup state the page already explains, not a toast.
function isLLMConfigError(message: string): boolean {
  const lower = message.toLowerCase();
  return (
    lower.includes('llm service is not available') ||
    lower.includes('configure an llm provider') ||
    lower.includes('llm provider')
  );
}

// Summary completed - stop spinners and update file
function onSummaryComplete(n: Notification, ctx: Ctx): void {
  ctx.setSummaryGenerating(false);
  ctx.setGeneratingSummary(false);
  // Reset reprocessing flag when summary completes (final step of reprocessing)
  ctx.setReprocessing(false);

  const file = ctx.getFile();
  if (!file) return;
  // The notification contains a brief preview, not the full summary_data. It no longer carries
  // `summary_opensearch_id` — the transcript_summaries index is retired (#67) — and the preview
  // is always sent on completion, so it alone establishes that a summary now exists.
  if (n.data?.summary) {
    file.has_summary = true; // Full data is fetched via API
  }
  // Force reactivity update by creating new object reference
  ctx.setFile({ ...file });
}

// Summary failed - stop spinners and show error
function onSummaryFailed(n: Notification, ctx: Ctx): void {
  ctx.setSummaryGenerating(false);
  ctx.setGeneratingSummary(false);
  const errorMessage =
    n.data?.message || n.message || ctx.t('fileDetail.failedToGenerateSummaryGeneric');
  if (!isLLMConfigError(errorMessage)) {
    ctx.toastError(errorMessage, 5000);
  }
}

function onSummarization(n: Notification, ctx: Ctx): void {
  // Only process notifications for the current file
  if (String(n.data?.file_id || '') !== String(ctx.getFileId() || '')) return;

  const status = statusOf(n);
  if (status === 'queued' || status === 'processing' || status === 'generating') {
    // Summary generation started - show spinner only if LLM is available
    const available = ctx.getLlmAvailable();
    ctx.setSummaryGenerating(available);
    ctx.setGeneratingSummary(available);
  } else if (status !== undefined && COMPLETED_STATUSES.has(status)) {
    onSummaryComplete(n, ctx);
  } else if (status === 'failed' || status === 'error') {
    onSummaryFailed(n, ctx);
  }
}

// Topic extraction produces AI suggestions for tags/collections.
function onTopicExtraction(n: Notification, ctx: Ctx): void {
  const status = statusOf(n);
  const message = n.message || n.data?.message;

  if (status === 'processing') {
    // Update processing step to show what's happening
    ctx.setCurrentProcessingStep(message || ctx.t('fileDetail.analyzingTranscript'));
  } else if (status === 'completed') {
    // Show completion message briefly before clearing
    ctx.setCurrentProcessingStep(message || ctx.t('fileDetail.aiSuggestionsComplete'));

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

// Content-redaction status: the chip, and an auto-refresh of the transcript.
function onRedaction(n: Notification, ctx: Ctx): void {
  const status = statusOf(n);
  ctx.setRedactionStatus(status || ctx.getRedactionStatus());
  if (status === 'done') {
    // Detection finished — load the now-available (masked) transcript.
    ctx.setRedactionPending(false);
    ctx.fetchFileDetails();
    const skipped = n.data?.skipped_detectors || [];
    if (skipped.length) {
      ctx.toastInfo(
        ctx.t('settings.contentRedaction.someDetectorsSkipped', { detectors: skipped.join(', ') })
      );
    }
  } else if (status === 'failed') {
    ctx.setRedactionPending(false);
    ctx.fetchFileDetails();
  } else {
    ctx.setRedactionPending(true);
  }
}

// Cache invalidation (e.g. auto-label applied tags/collections).
function onCacheInvalidate(n: Notification, ctx: Ctx): void {
  if (n.data?.scope !== 'files' || n.data?.file_id !== ctx.getFileId()) return;
  ctx.fetchFileDetails();
  Promise.resolve(ctx.loadAISuggestions()).catch((err) => {
    console.error('Error reloading AI suggestions after cache invalidate:', err);
  });
}

// `speaker_updated` and `speaker_processing_complete` are handled by window listeners in
// the page, not here: `stores/websocket.ts` dispatches them as CustomEvents and returns
// before the notification reaches the store, so a branch for them here can never run.
const NOTIFICATION_HANDLERS: Record<string, (n: Notification, ctx: Ctx) => void> = {
  transcript_ready: (_n, ctx) => onTranscriptReady(ctx),
  transcription_status: onTranscription,
  summarization_status: onSummarization,
  topic_extraction_status: onTopicExtraction,
  redaction_status: onRedaction,
  cache_invalidate: onCacheInvalidate,
};

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
  const handler = Object.hasOwn(NOTIFICATION_HANDLERS, latestNotification.type)
    ? NOTIFICATION_HANDLERS[latestNotification.type]
    : undefined;
  handler?.(latestNotification, ctx);
}
