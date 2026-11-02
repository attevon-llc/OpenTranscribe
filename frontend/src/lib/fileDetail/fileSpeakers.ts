/**
 * Every speaker flow of the file-detail page behind one context: loading the list, renaming
 * (single, profile decision, bulk save), the full refresh after a merge or a deleted speaker,
 * and the live CustomEvents. Composes `speakerLoader`, `speakerSave` and `speakerEvents`, so
 * the page wires its state once instead of once per module.
 */
import axiosInstance from '$lib/axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import { redactParams } from './segmentPaging';
import type { SegmentBearingFile } from './segmentSync';
import { listenForSpeakerEvents } from './speakerEvents';
import { createSpeakerLoader } from './speakerLoader';
import { createSpeakerSaveFlow, type SpeakerSaveContext } from './speakerSave';

export interface FileSpeakersContext extends Omit<SpeakerSaveContext, 'loadSpeakers'> {
  getFileId: () => string;
  getShowOriginal: () => boolean;
  /** Apply a full `GET /files/{id}` reload (collections, then the transcript's speakers). */
  applyReloadedFile: (data: SegmentBearingFile) => void;
  /** See `segmentPaging.refetchAfterMerge`. */
  refetchSegmentsAfterMerge: () => unknown;
}

export function createFileSpeakers(ctx: FileSpeakersContext) {
  const { loadSpeakers } = createSpeakerLoader(ctx);
  const save = createSpeakerSaveFlow({ ...ctx, loadSpeakers });

  /**
   * Refresh everything a merge (or a speaker deleted by segment reassignment) touches —
   * transcript, analytics, the editor's and merge panel's lists — without a loading spinner.
   * Honours the reveal flag: without it, merging while "show original" is on re-masks the
   * transcript. A bare `loadSpeakers()` did not keep those panels in sync.
   */
  async function refreshAfterMerge(): Promise<void> {
    const file = ctx.getFile();
    if (!file?.uuid) return;

    try {
      const response = await axiosInstance.get(`/files/${file.uuid}`, {
        params: redactParams(ctx.getShowOriginal()),
      });
      if (response.data && typeof response.data === 'object') {
        ctx.applyReloadedFile(response.data);
      }
      await loadSpeakers();
    } catch (error) {
      console.error('Error refreshing data after speaker merge:', error);
      toastStore.error(get(t)('fileDetail.dataRefreshFailed'));
    }
  }

  /** Start the speaker CustomEvent listeners; returns the function that stops them. */
  function listen(): () => void {
    return listenForSpeakerEvents({
      getFileId: ctx.getFileId,
      getSpeakerList: ctx.getSpeakerList,
      loadSpeakers,
      applySpeakerRename: save.applySpeakerRename,
      refetchSegmentsAfterMerge: ctx.refetchSegmentsAfterMerge,
      refreshCaptionTrack: ctx.refreshCaptionTrack,
    });
  }

  return { ...save, loadSpeakers, refreshAfterMerge, listen };
}
