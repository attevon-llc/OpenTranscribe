/**
 * Loads the file-detail page's speaker list (`GET /speakers?file_uuid=`) and, in the
 * background, the "appears in N other videos" chips.
 *
 * The page keeps owning the state (prop-drilling is the settled pattern, #338); this module
 * reads and writes it through {@link SpeakerLoaderContext}, as `notificationHandler` does.
 */
import { get } from 'svelte/store';
import axiosInstance from '$lib/axios';
import type { TranscriptSegment } from '$lib/types/media';
import { t } from '$stores/locale';
import { transcriptStore } from '$stores/transcriptStore';
import {
  baselineFrom,
  speakersFromSegments,
  unsavedNamesToKeep,
  type SpeakerItem,
  type SpeakerNameBaseline,
} from './speakerNames';

/** The parts of the page's `file` the speaker flows read. */
export interface SpeakerFile {
  uuid?: string;
  transcript_segments?: TranscriptSegment[];
  diarization_disabled?: boolean;
  [key: string]: unknown;
}

export interface SpeakerLoaderContext {
  getFile: () => SpeakerFile | null;
  getSpeakerList: () => SpeakerItem[];
  setSpeakerList: (speakers: SpeakerItem[]) => void;
  getBaseline: () => SpeakerNameBaseline;
  setBaseline: (baseline: SpeakerNameBaseline) => void;
}

/** Feed the transcript store (TranscriptViewModal / SpeakerStats read it, not `file`). */
export function syncTranscriptStore(file: SpeakerFile | null, speakers: SpeakerItem[]): void {
  if (file?.uuid && file.transcript_segments) {
    transcriptStore.loadTranscriptData(file.uuid, file.transcript_segments, speakers);
  }
}

export function createSpeakerLoader(ctx: SpeakerLoaderContext) {
  // Generation counter. Renames, websocket events and the initial load can all be in
  // flight at once; a stale response must not overwrite a newer one.
  let speakerLoadSeq = 0;

  /**
   * Load cross-media appearances ("appears in N other videos") for labeled speakers.
   *
   * Runs in the background: this is one request per already-labeled speaker, and it feeds
   * only the chips in the speaker editor. Awaiting it used to gate the transcript repaint
   * and the success toast behind N round trips, which is what made renaming feel frozen.
   *
   * `seq` is the generation of the `loadSpeakers` call that started this; results from a
   * superseded generation are dropped.
   */
  async function loadCrossMediaDataForLabeledSpeakers(seq: number): Promise<void> {
    const speakerList = ctx.getSpeakerList();
    if (!speakerList || speakerList.length === 0) return;
    if (ctx.getFile()?.diarization_disabled) return; // Skip cross-media for monologue files

    // Find speakers that need cross-media data (labeled speakers without individual matches)
    const speakersNeedingCrossMedia = speakerList.filter((speaker) => speaker.needsCrossMediaCall);
    if (speakersNeedingCrossMedia.length === 0) return;

    // Collect into a map rather than writing onto the captured speaker objects: by the
    // time these resolve, `loadSpeakers` may have replaced the list wholesale and those
    // writes would land on orphaned objects, silently losing the chips.
    const matchesByUuid = new Map<string, unknown[]>();

    await Promise.allSettled(
      speakersNeedingCrossMedia.map(async (speaker) => {
        try {
          const response = await axiosInstance.get(`/speakers/${speaker.uuid}/cross-media`);
          matchesByUuid.set(speaker.uuid, response.data || []);
        } catch (error) {
          console.error(`Error loading cross-media data for speaker ${speaker.uuid}:`, error);
          matchesByUuid.set(speaker.uuid, []);
        }
      })
    );

    if (seq !== speakerLoadSeq) return; // A newer load won — discard.

    const updated = ctx
      .getSpeakerList()
      .map((speaker) =>
        matchesByUuid.has(speaker.uuid)
          ? { ...speaker, cross_video_matches: matchesByUuid.get(speaker.uuid) }
          : speaker
      );
    ctx.setSpeakerList(updated);

    // Update transcript store with the new cross-media data
    syncTranscriptStore(ctx.getFile(), updated);
  }

  function useSegmentFallback(): void {
    const segments = ctx.getFile()?.transcript_segments;
    if (segments) {
      const speakers = speakersFromSegments(segments, get(t)('fileDetail.unknownSpeaker'));
      ctx.setSpeakerList(speakers);
      syncTranscriptStore(ctx.getFile(), speakers);
    }
  }

  /** Load speakers for the current file. */
  async function loadSpeakers(): Promise<void> {
    const file = ctx.getFile();
    if (!file?.uuid) return;
    const seq = ++speakerLoadSeq;

    try {
      const response = await axiosInstance.get(`/speakers`, {
        params: { file_uuid: file.uuid },
      });
      if (seq !== speakerLoadSeq) return; // Superseded by a newer load.

      if (response.data && Array.isArray(response.data)) {
        // A websocket event or a sibling rename can land while the user is typing in
        // another speaker's field, and this assignment replaces the whole list. Carry
        // unsaved edits across so they aren't silently wiped mid-keystroke.
        const unsavedNames = unsavedNamesToKeep(ctx.getSpeakerList(), ctx.getBaseline());

        // Use pre-processed data directly from backend - no frontend business logic
        const speakers: SpeakerItem[] = response.data.map((speaker: SpeakerItem) => ({
          ...speaker,
          verified: speaker.verified ?? false,
          display_name: unsavedNames.has(speaker.uuid)
            ? unsavedNames.get(speaker.uuid)
            : speaker.display_name,
          showMatches: false, // Only UI state, not business logic
          showSuggestions: false, // Only UI state, not business logic
        }));
        ctx.setSpeakerList(speakers);

        // Change-detection baseline, built from SERVER values so a preserved edit above
        // still reads as unsaved.
        ctx.setBaseline(baselineFrom(response.data));

        syncTranscriptStore(ctx.getFile(), speakers);

        // Cross-media feeds a secondary panel only — fetch it after the render above,
        // and don't make callers wait for it.
        void loadCrossMediaDataForLabeledSpeakers(seq);
      } else {
        useSegmentFallback();
      }
    } catch (error) {
      console.error('Error loading speakers:', error);
      useSegmentFallback();
    }
  }

  return { loadSpeakers };
}
