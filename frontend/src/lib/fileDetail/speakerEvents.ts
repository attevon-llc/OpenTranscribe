/**
 * Window CustomEvents (dispatched by `stores/websocket.ts`, never stored) that keep an open
 * file-detail page's speakers current.
 */
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { toastStore } from '$stores/toast';
import type { SpeakerItem } from './speakerNames';

export interface SpeakerEventContext {
  getFileId: () => string;
  getSpeakerList: () => SpeakerItem[];
  loadSpeakers: () => unknown;
  applySpeakerRename: (
    speakerUuid: string,
    speakerLabel: string | undefined,
    newName: string
  ) => void;
  /** See `segmentPaging.refetchAfterMerge`. */
  refetchSegmentsAfterMerge: () => unknown;
  refreshCaptionTrack: (reason: string) => void;
}

type Detail = Record<string, unknown> | null | undefined;

/** Start listening; returns the function that stops. */
export function listenForSpeakerEvents(ctx: SpeakerEventContext): () => void {
  // speaker-updated: gender detection, attribute changes.
  function onSpeakerUpdated(e: Event) {
    const detail = (e as CustomEvent).detail as Detail;
    // Publishers disagree on the field name: speaker_attribute_task sends `file_id`,
    // the speaker endpoints send `media_file_id`. Accept both.
    const eventFileId = detail?.file_id ?? detail?.media_file_id;
    if (eventFileId && String(eventFileId) === String(ctx.getFileId())) {
      ctx.loadSpeakers();
    }
  }

  /**
   * Eventual-consistency backstop for a speaker rename or merge.
   *
   * Renaming a speaker kicks off a background task that projects the change into
   * OpenSearch and retroactively labels matching speakers in other files. This event is
   * how those downstream effects reach an open page. It was dispatched but never listened
   * for, so auto-applied labels only appeared after a manual reload.
   */
  function onProcessingComplete(e: Event) {
    const detail = (e as CustomEvent).detail as Detail;
    if (!detail?.media_file_id || String(detail.media_file_id) !== String(ctx.getFileId())) return;

    if (detail.reason === 'speaker_merged') {
      // The merged-away speaker's uuid is gone from Postgres; there is no rename mapping
      // to patch segments in place with, so refetch rather than trying to guess.
      ctx.refetchSegmentsAfterMerge();
    } else if (detail.speaker_uuid && detail.display_name) {
      // Repaint the renamed speaker directly: loadSpeakers() refreshes the speaker list but
      // touches neither the segments nor the transcript store.
      const speakerUuid = String(detail.speaker_uuid);
      const speakerLabel = ctx.getSpeakerList().find((s) => s.uuid === speakerUuid)?.name;
      ctx.applySpeakerRename(speakerUuid, speakerLabel, String(detail.display_name));
    }

    // Picks up labels the task auto-applied to OTHER speakers in this file.
    ctx.loadSpeakers();
    ctx.refreshCaptionTrack('speaker processing');

    const autoApplied = (detail.auto_applied_count as number) || 0;
    const suggested = (detail.suggested_count as number) || 0;
    if (autoApplied > 0) {
      toastStore.info(get(t)('speakerProfile.autoAppliedToOthers', { count: autoApplied }));
    } else if (suggested > 0) {
      toastStore.info(get(t)('speakerProfile.suggestionsCreated', { count: suggested }));
    }
  }

  window.addEventListener('speaker-updated', onSpeakerUpdated);
  window.addEventListener('speaker-processing-complete', onProcessingComplete);
  return () => {
    window.removeEventListener('speaker-updated', onSpeakerUpdated);
    window.removeEventListener('speaker-processing-complete', onProcessingComplete);
  };
}
