import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { formatFileSize } from '$lib/utils/formatting';
import { MEDIA_TYPE_BY_EXTENSION, fileExtension } from '$lib/utils/mediaFormats';
import {
  getMaxUploadBytes,
  exceedsUploadLimit,
  warrantsLargeUploadWarning,
} from '$lib/utils/uploadLimits';
import type { UploadTranscriptionParams } from '$lib/services/uploadService';

export const MEDIA_URL_REGEX = /^https?:\/\/.+$/;

/**
 * Check one file picked in the single-file wizard.
 *
 * @returns `file: null` with an error when it cannot be used; otherwise the file, plus a
 *   soft warning for very large ones (accepted, just slow).
 */
export function validateSelectedFile(selected: File): { ok: boolean; error: string } {
  const tr = get(t);
  if (!selected.type || !selected.type.match(/^(audio|video)\//)) {
    return { ok: false, error: tr('uploader.fileTypeError') };
  }
  if (exceedsUploadLimit(selected.size)) {
    // Only reachable when the live limit is a real ceiling (exceedsUploadLimit is
    // false whenever getMaxUploadBytes() is null, i.e. the admin disabled it).
    return {
      ok: false,
      error: tr('uploader.fileTooLargeError', {
        fileSize: formatFileSize(selected.size),
        maxSize: formatFileSize(getMaxUploadBytes() ?? 0),
      }),
    };
  }
  if (warrantsLargeUploadWarning(selected.size)) {
    return {
      ok: true,
      error: tr('uploader.largeFileWarning', { fileSize: formatFileSize(selected.size) }),
    };
  }
  return { ok: true, error: '' };
}

/** Split a multi-file drop into uploadable files and human-readable rejections. */
export function partitionMultipleFiles(files: File[]): {
  validFiles: File[];
  invalidFiles: string[];
} {
  const tr = get(t);
  const validFiles: File[] = [];
  const invalidFiles: string[] = [];

  files.forEach((f) => {
    if (exceedsUploadLimit(f.size)) {
      invalidFiles.push(`${f.name} (${tr('uploader.tooLarge', { size: formatFileSize(f.size) })})`);
      return;
    }
    const isValidType = f.type && (f.type.startsWith('audio/') || f.type.startsWith('video/'));
    if (!isValidType && !(fileExtension(f.name) in MEDIA_TYPE_BY_EXTENSION)) {
      invalidFiles.push(`${f.name} (${tr('uploader.unsupportedFormat')})`);
      return;
    }
    validFiles.push(f);
  });
  return { validFiles, invalidFiles };
}

/** Body for `POST /files/process-url` from the wizard's choices (extras = auth/quality from the URL panel). */
export function buildUrlPayload(
  url: string,
  extras: Record<string, unknown> | null,
  organize: { collectionIds?: string[]; tagNames?: string[] },
  skipSummary: boolean,
  params: UploadTranscriptionParams
): Record<string, unknown> {
  const payload: Record<string, unknown> = { url: url.trim() };
  if (extras) Object.assign(payload, extras);
  if (organize.collectionIds) payload.collection_ids = organize.collectionIds;
  if (organize.tagNames) payload.tag_names = organize.tagNames;
  if (skipSummary) payload.skip_summary = true;
  if (params.minSpeakers != null) payload.min_speakers = params.minSpeakers;
  if (params.maxSpeakers != null) payload.max_speakers = params.maxSpeakers;
  if (params.numSpeakers != null) payload.num_speakers = params.numSpeakers;
  if (params.whisperModel) payload.whisper_model = params.whisperModel;
  return payload;
}

/** Toast kind and message for a failed `process-url` call. */
export function urlErrorToast(error: unknown): { kind: 'warning' | 'error'; message: string } {
  const tr = get(t);
  const e = error as { response?: { status?: number; data?: { detail?: string } } };
  if (e.response?.status === 409) {
    return {
      kind: 'warning',
      message: e.response.data?.detail || tr('uploader.duplicateMediaVideo'),
    };
  }
  if (e.response?.status === 400) {
    return { kind: 'error', message: e.response.data?.detail || tr('uploader.invalidMediaUrl') };
  }
  return { kind: 'error', message: tr('uploader.failedToProcessUrl') };
}
