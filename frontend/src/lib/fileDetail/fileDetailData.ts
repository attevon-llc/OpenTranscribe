/**
 * Reads behind the file-detail page: `GET /files/{id}` (with the gallery prefetch join),
 * the transcript-only refreshes, analytics, and the per-file redaction rescan.
 *
 * These return data; the page assigns it. The caller's permission (`my_permission`) is
 * deliberately NOT mapped here: the page reads it inline where it decides the edit and
 * show-original gates, so the rule stays in one visible place.
 */
import type { MediaFileDetail } from '$lib/types/media';
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { apiCache, cacheKey, CacheTTL } from '$lib/apiCache';
import {
  getAISuggestions,
  type CollectionSuggestion,
  type TagSuggestion,
} from '$lib/api/suggestions';
import { t } from '$stores/locale';
import { getErrorMessage } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';
import { redactParams } from './segmentPaging';

/** The `GET /files/{id}` payload, open-ended: the page passes it to many children. */
export type FileDetailPayload = Record<string, unknown> & {
  transcript_segments?: Array<{ redactions?: unknown[] | null } | null>;
};

/**
 * @param fromPrefetch First load of the page only. Joins (or consumes) the gallery's hover
 *   prefetch of `GET /files/{id}` instead of issuing a second identical request; the entry
 *   is dropped once read so a later reload (reprocess, retry) always hits the server.
 */
export async function getFileDetail(
  fileId: string,
  opts: { fromPrefetch: boolean; showOriginal: boolean }
): Promise<unknown> {
  if (opts.fromPrefetch && !opts.showOriginal) {
    const key = cacheKey.fileDetail(fileId);
    const data = await apiCache.getOrFetch(
      key,
      () => axiosInstance.get(`/files/${fileId}`).then((r) => r.data),
      CacheTTL.FILES
    );
    apiCache.invalidate(key);
    return data;
  }
  const response = await axiosInstance.get(`/files/${fileId}`, {
    params: redactParams(opts.showOriginal),
  });
  return response.data;
}

/** Content-redaction state: pending (transcript withheld) and whether masking applies. */
export function readRedactionState(data: FileDetailPayload) {
  return {
    redactionPending: (data.redaction_pending as boolean) || false,
    redactionStatus: (data.redaction_status as string) || '',
    redactionEnabled: data.redaction_enabled === true,
  };
}

/**
 * Whether this masked read carries redacted spans. Only ever turns the page's
 * `redactionActive` ON: an unmasked ("show original") read has no spans to see.
 */
export function sawMaskedSpans(data: FileDetailPayload, showOriginal: boolean): boolean {
  return (
    !showOriginal &&
    Array.isArray(data.transcript_segments) &&
    data.transcript_segments.some((s) => s?.redactions?.length)
  );
}

export function paginationFrom(data: FileDetailPayload) {
  return {
    totalSegments: (data.total_segments as number) || 0,
    totalSpeakerSegments: (data.total_speaker_segments as number) || 0,
    segmentLimit: (data.segment_limit as number) || 500,
  };
}

/**
 * The `transcript_ready` refresh: replace the transcript and processing fields of the
 * current file while keeping everything else (UI flags, title edits). Both segment
 * representations are replaced together — the transcript renders from `grouped_segments`,
 * so a stale copy keeps showing the old transcript (#352).
 */
export function mergeTranscriptRefresh<T extends Record<string, unknown>>(
  file: T,
  data: FileDetailPayload
): T {
  const next: Record<string, unknown> = {
    ...file,
    transcript_segments: data.transcript_segments || [],
    grouped_segments: data.grouped_segments || [],
    speakers: data.speakers || [],
    waveform_data: data.waveform_data,
    duration: data.duration,
    duration_seconds: data.duration_seconds,
    processed_at: data.processed_at,
    analytics: data.analytics,
  };
  // Persist ASR provider/model so MetadataDisplay can show them after transcription
  // completes without requiring a full page refresh. Absent means "unchanged".
  for (const key of ['asr_provider', 'asr_model', 'language', 'diarization_disabled']) {
    if (data[key] !== undefined) next[key] = data[key];
  }
  return next as T;
}

/** Only the analytics (lightweight, no full file detail); `null` when there is nothing to apply. */
export async function fetchFileAnalytics(
  fileUuid: string
): Promise<{ analytics: MediaFileDetail['analytics'] } | null> {
  try {
    const response = await axiosInstance.get(`/files/${fileUuid}/analytics`);
    if (response.data && typeof response.data === 'object') {
      return { analytics: response.data.analytics };
    }
  } catch (error) {
    // Not critical to the user's workflow, so no toast.
    console.error('Error refreshing analytics after speaker change:', error);
  }
  return null;
}

/**
 * Trigger (or re-run) content-redaction detection for one file. Useful for old files
 * processed before redaction was enabled, or to re-scan after changing settings.
 * Toasts the outcome; resolves `true` when the scan was queued.
 */
export async function startRedactionScan(fileUuid: string): Promise<boolean> {
  const tr = get(t);
  try {
    const response = await axiosInstance.post('/files/management/bulk-action', {
      file_uuids: [fileUuid],
      action: 'redact',
    });
    const result = (response.data || [])[0];
    if (result && result.success) {
      toastStore.success(tr('settings.contentRedaction.redactStarted'));
      return true;
    }
    toastStore.error(result?.message || tr('settings.contentRedaction.redactTriggerFailed'));
  } catch (err) {
    console.error('Trigger redaction error:', err);
    // The server's reason (the 409 "redaction is not enabled", a support-session refusal) beats a generic failure.
    toastStore.error(getErrorMessage(err, tr('settings.contentRedaction.redactTriggerFailed')));
  }
  return false;
}

/**
 * Stored AI tag/collection suggestions, loaded whatever the current LLM configuration, so
 * suggestions generated earlier stay usable after the LLM is switched off. `null` when there
 * is nothing to show: rejected, none yet (a 404 is the normal answer), or the read failed.
 */
export async function fetchAISuggestions(
  fileId: string
): Promise<{ tags: TagSuggestion[]; collections: CollectionSuggestion[] } | null> {
  try {
    const suggestions = await getAISuggestions(fileId);
    if (suggestions && suggestions.status !== 'rejected') {
      return { tags: suggestions.tags || [], collections: suggestions.collections || [] };
    }
  } catch (error) {
    console.error('Error loading AI suggestions:', error);
  }
  return null;
}
