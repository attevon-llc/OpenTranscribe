/**
 * The owner's "show original" toggle on the file-detail page: flip the reveal flag and swap
 * the transcript text in place. Whether the toggle is OFFERED is the page's decision
 * (`canViewOriginal`); the server still keeps admin-forced categories masked regardless.
 */
import axiosInstance from '$lib/axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import type { FileDetailPayload } from './fileDetailData';
import { redactParams } from './segmentPaging';
import type { SegmentBearingFile } from './segmentSync';

export interface RedactionViewContext {
  getFileId: () => string;
  getFile: () => SegmentBearingFile | null;
  /** Must perform the page's real `file = ...` assignment. */
  setFile: (file: SegmentBearingFile) => void;
  getShowOriginal: () => boolean;
  setShowOriginal: (showOriginal: boolean) => void;
  isToggleBusy: () => boolean;
  setToggleBusy: (busy: boolean) => void;
  /** Apply the redaction fields of the fresh read (pending, status, enabled, active). */
  applyRedactionState: (data: FileDetailPayload) => void;
  /** The transcript was replaced: reload what derives from it. */
  onTranscriptReloaded: () => void;
}

export function createRedactionView(ctx: RedactionViewContext) {
  /**
   * Re-fetch transcript segments (honouring the current redact flag) and update
   * transcript-related state in place. Everything else on the page — player,
   * metadata, analytics — is left untouched.
   */
  async function refreshTranscriptOnly(): Promise<void> {
    try {
      const response = await axiosInstance.get(`/files/${ctx.getFileId()}`, {
        params: redactParams(ctx.getShowOriginal()),
      });
      if (response.data && typeof response.data === 'object') {
        // Replace BOTH segment representations: TranscriptDisplay prefers the
        // backend-pre-grouped `grouped_segments`, so leaving the stale copy in
        // place would keep rendering the old (masked/unmasked) text.
        ctx.setFile({
          ...ctx.getFile(),
          transcript_segments: response.data.transcript_segments,
          grouped_segments: response.data.grouped_segments,
        });
        ctx.applyRedactionState(response.data);
        ctx.onTranscriptReloaded();
      }
    } catch (error) {
      console.error('Error refreshing transcript:', error);
      toastStore.error(get(t)('fileDetail.failedToLoadFile'));
    }
  }

  /**
   * Toggle between the redacted view and the original. Refreshes ONLY the transcript data
   * — no loading flip, no player re-setup — so the text swaps without a full-page flicker.
   */
  async function toggleShowOriginal(): Promise<void> {
    if (ctx.isToggleBusy()) return;
    ctx.setToggleBusy(true);
    ctx.setShowOriginal(!ctx.getShowOriginal());
    try {
      await refreshTranscriptOnly();
    } finally {
      ctx.setToggleBusy(false);
    }
  }

  return { toggleShowOriginal };
}
