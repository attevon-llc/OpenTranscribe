/**
 * The file-detail page's inline segment editor: open / save / cancel, and invalidating what
 * is derived from the transcript once a write commits.
 *
 * The page keeps the editor state (it is bound into `TranscriptDisplay`, and an edit in
 * progress must survive the transcript column re-rendering during a reload); this module
 * reads and writes it through {@link SegmentEditorContext}.
 */
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import { getErrorStatus } from '$lib/utils/apiError';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { transcriptStore } from '$stores/transcriptStore';
import { reportHoldRefusal } from '$lib/legalHold/refusal';
import { patchSegmentInFile, type SegmentBearingFile } from './segmentSync';

/** Which segment is open in the editor, its current text, and the text it opened with. */
export interface SegmentEditState {
  id: string | number | null;
  text: string;
  originalText: string;
}

export const CLOSED_SEGMENT_EDIT: SegmentEditState = { id: null, text: '', originalText: '' };

/**
 * An open editor only counts as unsaved once the text actually differs — opening one and
 * closing the tab must not prompt. Trimmed on both sides so trailing whitespace the textarea
 * introduced is not "an edit".
 */
export function hasUnsavedSegmentEdit(edit: SegmentEditState): boolean {
  return edit.id !== null && edit.text.trim() !== edit.originalText.trim();
}

/**
 * `PUT /files/{fileId}/transcript/segments/{segmentUuid}`. Resolves the server's row (with
 * the text it stored), or `null` for an empty response; rejects on any failure.
 */
export async function saveSegmentText(
  fileId: string,
  segmentUuid: string,
  text: string
): Promise<{ text?: string } | null> {
  const response = await axiosInstance.put(`/files/${fileId}/transcript/segments/${segmentUuid}`, {
    text,
  });
  return response.data || null;
}

/** The i18n key telling the reader why a segment save failed. */
export function segmentSaveErrorKey(status: number | undefined): string {
  if (status === 405) return 'fileDetail.transcriptEditingNotSupported';
  if (status === 404) return 'fileDetail.transcriptSegmentNotFound';
  if (status === 422) return 'fileDetail.invalidSegmentData';
  return 'fileDetail.failedToSaveSegment';
}

/**
 * Drop the server's cached processed videos (burned-in subtitles) so the next download
 * uses the updated transcript. Fire-and-forget: a failure only costs a stale cache.
 */
export function clearProcessedVideoCache(fileUuid: unknown): void {
  axiosInstance.delete(`/files/${fileUuid}/cache`).catch((error: unknown) => {
    console.warn('Could not clear video cache:', error);
  });
}

export interface SegmentEditorContext {
  getFileId: () => string;
  getFile: () => SegmentBearingFile | null;
  /** Must perform the page's real `file = ...` assignment. */
  setFile: (file: SegmentBearingFile) => void;
  getEdit: () => SegmentEditState;
  setEdit: (edit: SegmentEditState) => void;
  setSaving: (saving: boolean) => void;
  refreshCaptionTrack: (reason: string) => void;
  refreshAnalytics: () => void;
}

export function createSegmentEditor(ctx: SegmentEditorContext) {
  function open(segment: { uuid: string; text: string }) {
    ctx.setEdit({ id: segment.uuid, text: segment.text, originalText: segment.text ?? '' });
  }

  function cancel() {
    ctx.setEdit(CLOSED_SEGMENT_EDIT);
  }

  async function save(segment: { uuid: string } | null | undefined) {
    const text = ctx.getEdit().text;
    if (!segment || !text) return;

    try {
      ctx.setSaving(true);
      const segmentUuid = segment.uuid;
      const saved = await saveSegmentText(ctx.getFileId(), segmentUuid, text);

      if (saved) {
        // Update the transcript store FIRST for reactivity (its readers do not read `file`)
        transcriptStore.updateSegmentText(segmentUuid, text);

        // Patch only `text`, so the segment's speaker fields cannot be clobbered.
        const file = patchSegmentInFile(ctx.getFile() as SegmentBearingFile, segmentUuid, {
          text: saved.text ?? text,
        });
        ctx.setFile(file);

        // The write is committed — close the editor now. Everything below only affects
        // future downloads and the subtitle track, so it must not hold the UI open.
        cancel();
        clearProcessedVideoCache(file.uuid);
        ctx.refreshCaptionTrack('segment edit');

        // Word counts and talk-time are derived from segment text; refresh them.
        ctx.refreshAnalytics();
      }
    } catch (error: unknown) {
      console.error('Error saving segment:', error);
      // A hold placed since the page loaded: the translated refusal, and the editor closes.
      if (reportHoldRefusal(error)) cancel();
      else toastStore.error(get(t)(segmentSaveErrorKey(getErrorStatus(error))));
    } finally {
      ctx.setSaving(false);
    }
  }

  return { open, save, cancel };
}
