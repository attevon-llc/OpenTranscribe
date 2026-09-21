import { writable } from 'svelte/store';

export interface TranscriptSegment {
  uuid: string; // UUID identifier
  start_time: number;
  end_time: number;
  text: string;
  confidence?: number; // ASR confidence score (0.0 - 1.0)
  speaker_id?: string; // UUID
  speaker_label?: string;
  resolved_speaker_name?: string;
  speaker?: {
    uuid: string; // UUID
    name: string;
    display_name?: string;
  };
  formatted_timestamp?: string;
  display_timestamp?: string;
  // Overlap fields for simultaneous speech display
  is_overlap?: boolean;
  overlap_group_id?: string;
  overlap_confidence?: number;
}

export interface SpeakerInfo {
  uuid: string; // UUID (public identifier)
  name: string; // Original speaker ID (e.g., "SPEAKER_01")
  display_name?: string; // User-assigned display name
  verified: boolean;
}

export interface TranscriptData {
  fileId: string | null; // UUID
  segments: TranscriptSegment[];
  speakers: SpeakerInfo[];
}

// Create the writable store
const createTranscriptStore = () => {
  const { subscribe, set, update } = writable<TranscriptData>({
    fileId: null,
    segments: [],
    speakers: [],
  });

  return {
    subscribe,
    // Load initial data for a file
    loadTranscriptData: (
      fileId: string,
      segments: TranscriptSegment[],
      speakers: SpeakerInfo[]
    ) => {
      set({
        fileId,
        segments: segments.map((segment) => ({ ...segment })),
        speakers: speakers.map((speaker) => ({ ...speaker })),
      });
    },

    // Update a speaker's display name
    updateSpeakerName: (speakerId: string, newDisplayName: string) => {
      update((data) => {
        // Update speaker in speakers array
        const updatedSpeakers = data.speakers.map((speaker) =>
          speaker.uuid === speakerId ? { ...speaker, display_name: newDisplayName } : speaker
        );

        // Update all segments for this speaker
        const updatedSegments = data.segments.map((segment) => {
          if (segment.speaker_id === speakerId) {
            return {
              ...segment,
              resolved_speaker_name: newDisplayName,
              speaker: segment.speaker
                ? {
                    ...segment.speaker,
                    uuid: segment.speaker.uuid,
                    name: segment.speaker.name, // Keep original name for color consistency
                    display_name: newDisplayName,
                  }
                : {
                    uuid: speakerId,
                    name: segment.speaker_label || `SPEAKER_${speakerId}`,
                    display_name: newDisplayName,
                  },
            };
          }
          return segment;
        });

        return {
          ...data,
          speakers: updatedSpeakers,
          segments: updatedSegments,
        };
      });
    },

    // Clear store when navigating away
    clear: () => {
      set({
        fileId: null,
        segments: [],
        speakers: [],
      });
    },

    // Update segments (for text edits, etc.)
    updateSegments: (newSegments: TranscriptSegment[]) => {
      update((data) => ({
        ...data,
        segments: newSegments.map((segment) => ({ ...segment })),
      }));
    },

    // Update a specific segment's text while preserving ALL other data
    updateSegmentText: (segmentUuid: string, newText: string) => {
      update((data) => {
        const updatedSegments = data.segments.map((segment) => {
          if (segment.uuid === segmentUuid) {
            // Preserve ALL existing segment data, only update text
            return {
              ...segment, // Keep all existing properties
              text: newText, // Only update the text field
              // Preserve: speaker_id, speaker_label, speaker, resolved_speaker_name, etc.
            };
          }
          return segment;
        });

        return {
          ...data,
          segments: updatedSegments,
        };
      });
    },
  };
};

// Export the store instance
export const transcriptStore = createTranscriptStore();

// `ProcessedSegment` and the `processedTranscriptSegments` derived store (the
// speaker-block "reading view" grouping) were deleted with `TranscriptModal.svelte`
// (issue #755, J10) — that was their only production consumer, and the consolidated
// transcript view renders the backend's own `grouped_segments` instead of recomputing
// grouping client-side. `AnalyticsSection` still consumes the raw `transcriptStore`
// above; that stays.
