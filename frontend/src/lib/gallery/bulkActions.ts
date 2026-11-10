/**
 * The gallery's bulk actions over the selection: pick the eligible files, post one request, toast
 * the outcome. Table-driven so a new action is a row, not another 30-line copy of the same flow.
 *
 * Every action sends ONE request for the whole selection (`POST /files/management/bulk-action`,
 * chunked by `postBulkAction`); the server answers a result per file.
 */
import axiosInstance from '$lib/axios';
import { postBulkAction, type BulkFileResult } from '$lib/api/bulkActions';
import { bulkLlmFailureKey } from '$lib/utils/bulkLlmOutcome';
import { getErrorMessage } from '$lib/utils/apiError';
import { triggerAnchorDownload } from '$lib/export/anchorDownload';
import { openExportJobStream, type ExportJobStream } from '$lib/export/exportJobStream';
import { toastStore } from '$stores/toast';
import type { MediaFile } from '$lib/types/media';

type Translate = (key: string, options?: Record<string, unknown>) => string;

export interface BulkActionSpec {
  /** `action` sent to the server. */
  action: string;
  /** Statuses a file must have to be eligible. */
  statuses: string[];
  /** Toast when nothing in the selection is eligible. */
  noneKey: string;
  startedKey: string;
  failedKey: string;
  /** Console label for a thrown request. */
  logLabel: string;
  /** Surface the server's error text on a thrown request, instead of the generic failure line. */
  serverError: boolean;
  /** "Unusable configuration" and "none configured" call for different next steps (LLM-backed actions). */
  llmCheck: boolean;
}

export const BULK_ACTIONS = {
  summarize: {
    action: 'summarize',
    statuses: ['completed'],
    noneKey: 'gallery.bulk.noCompletedFiles',
    startedKey: 'gallery.bulk.summarizeStarted',
    failedKey: 'gallery.bulk.summarizeFailed',
    logLabel: 'Bulk summarize error:',
    serverError: true,
    llmCheck: true,
  },
  redact: {
    action: 'redact',
    statuses: ['completed'],
    noneKey: 'gallery.bulk.noCompletedFiles',
    startedKey: 'gallery.bulk.redactStarted',
    failedKey: 'gallery.bulk.redactFailed',
    logLabel: 'Bulk redact error:',
    serverError: true,
    llmCheck: false,
  },
  retry: {
    action: 'retry',
    statuses: ['error'],
    noneKey: 'gallery.bulk.noFailedFiles',
    startedKey: 'gallery.bulk.retryStarted',
    failedKey: 'gallery.bulk.retryFailedError',
    logLabel: 'Bulk retry error:',
    serverError: false,
    llmCheck: false,
  },
  speakerId: {
    action: 'identify_speakers',
    statuses: ['completed'],
    noneKey: 'gallery.bulk.noCompletedFiles',
    startedKey: 'gallery.bulk.speakerIdStarted',
    failedKey: 'gallery.bulk.speakerIdFailed',
    logLabel: 'Bulk speaker ID error:',
    serverError: true,
    llmCheck: true,
  },
  cancel: {
    action: 'cancel',
    statuses: ['processing'],
    noneKey: 'gallery.bulk.noProcessingFiles',
    startedKey: 'gallery.bulk.cancelStarted',
    failedKey: 'gallery.bulk.cancelFailed',
    logLabel: 'Bulk cancel error:',
    serverError: false,
    llmCheck: false,
  },
} satisfies Record<string, BulkActionSpec>;

/** The selected files whose status is one of `statuses`, in gallery order. */
export function eligibleFiles(
  files: MediaFile[],
  selected: Set<string>,
  statuses: string[]
): MediaFile[] {
  return files.filter((f) => selected.has(f.uuid) && statuses.includes(f.status));
}

export async function runBulkAction(
  spec: BulkActionSpec,
  files: MediaFile[],
  selected: Set<string>,
  t: Translate
): Promise<void> {
  if (selected.size === 0) return;
  const targets = eligibleFiles(files, selected, spec.statuses);
  if (targets.length === 0) {
    toastStore.warning(t(spec.noneKey));
    return;
  }
  try {
    const results = await postBulkAction<BulkFileResult>({
      file_uuids: targets.map((f) => f.uuid),
      action: spec.action,
    });
    if (spec.llmCheck) {
      const llmFailureKey = bulkLlmFailureKey(results);
      if (llmFailureKey) {
        toastStore.error(t(llmFailureKey));
        return;
      }
    }
    const successful = results.filter((r) => r.success).length;
    const failed = results.length - successful;
    if (successful > 0) toastStore.success(t(spec.startedKey, { count: successful }));
    if (failed > 0) toastStore.error(t(spec.failedKey, { count: failed }));
  } catch (err) {
    console.error(spec.logLabel, err);
    const fallback = t(spec.failedKey, { count: targets.length });
    toastStore.error(spec.serverError ? getErrorMessage(err, fallback) : fallback);
  }
}

/**
 * Bulk export of the selection as one ZIP, built on the download worker and delivered as a
 * presigned URL over SSE (no archive bytes proxied through the API). One job at a time: a newer
 * export replaces the stream of an older one.
 */
export class BulkExporter {
  private stream: ExportJobStream | null = null;

  async run(
    format: string,
    files: MediaFile[],
    selected: Set<string>,
    t: Translate
  ): Promise<void> {
    if (selected.size === 0) return;
    const completed = eligibleFiles(files, selected, ['completed']);
    if (completed.length === 0) {
      toastStore.warning(t('gallery.bulk.noCompletedFiles'));
      return;
    }
    const ext = format === 'webvtt' ? 'vtt' : format;
    const total = completed.length;
    toastStore.info(t('gallery.bulk.exportStarted', { count: total, format: ext.toUpperCase() }));

    try {
      const { data } = await axiosInstance.post('/files/bulk-export/prepare', {
        file_uuids: completed.map((f) => f.uuid),
        subtitle_format: format,
        include_speakers: true,
      });
      this.stream?.close();
      const stream = openExportJobStream(data.job_id);
      this.stream = stream;
      try {
        const d = await stream.result;
        triggerAnchorDownload(d.url, d.filename ?? `transcripts_${format}.zip`);
        toastStore.success(
          t('gallery.bulk.exportComplete', {
            count: d.exported || total,
            format: ext.toUpperCase(),
          })
        );
        if (d.skipped > 0)
          toastStore.warning(t('gallery.bulk.exportSkipped', { count: d.skipped }));
      } catch {
        toastStore.error(t('gallery.bulk.exportFailed', { count: total }));
      } finally {
        if (this.stream === stream) this.stream = null;
      }
    } catch (err) {
      console.error('Bulk export failed to start:', err);
      toastStore.error(t('gallery.bulk.exportFailed', { count: total }));
    }
    // The selection stays: the user may export in several formats.
  }

  /** Stop listening to any in-flight export (the page is going away). */
  close(): void {
    this.stream?.close();
  }
}
