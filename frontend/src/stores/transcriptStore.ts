import { writable, derived } from 'svelte/store';

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

// Type for processed segments with overlap info
export interface ProcessedSegment {
  speakerName: string;
  speaker_label: string;
  text: string;
  startTime: number;
  endTime: number;
  rawStartIndex: number; // Index of the first raw segment in this processed block
  rawSegmentCount: number; // Number of raw segments merged into this block
  isOverlapGroup?: boolean;
  overlapGroupId?: string;
  overlapSegments?: ProcessedSegment[];
}

// Derived store for processed segments suitable for TranscriptModal display
export const processedTranscriptSegments = derived(transcriptStore, ($transcriptStore) => {
  if (!$transcriptStore.segments.length) {
    return [];
  }

  // Sort segments by start_time
  const sortedSegments = [...$transcriptStore.segments].sort((a, b) => {
    const aStart = parseFloat(String(a.start_time || 0));
    const bStart = parseFloat(String(b.start_time || 0));
    return aStart - bStart;
  });

  // Group consecutive segments from the same speaker for display
  // IMPORTANT: Do not merge segments with different overlap_group_id
  // Track raw segment indices so progress bar maps to total transcript length
  const groupedSegments: ProcessedSegment[] = [];
  let currentSpeaker: string | null = null;
  let currentSpeakerLabel: string | null = null;
  let currentText: string[] = [];
  let currentStartTime: number | null = null;
  let currentEndTime: number | null = null;
  let currentOverlapGroupId: string | null | undefined = null;
  let currentRawStartIndex = 0;
  let currentRawCount = 0;

  sortedSegments.forEach((segment, rawIndex) => {
    // Backend guarantees `resolved_speaker_name` (display_name → speaker_label →
    // "Unknown"); prefer it directly, keeping a last-resort guard for old payloads.
    const speakerName = segment.resolved_speaker_name || 'Unknown Speaker';
    const speakerLabel = segment.speaker_label || segment.speaker?.name || 'Unknown';
    const startTime = parseFloat(String(segment.start_time || 0));
    const endTime = parseFloat(String(segment.end_time || 0));
    const overlapGroupId = segment.overlap_group_id;

    // Check if we should start a new group
    // Start new group if speaker changes OR if overlap_group_id changes
    const shouldStartNewGroup =
      speakerName !== currentSpeaker || overlapGroupId !== currentOverlapGroupId;

    if (shouldStartNewGroup) {
      if (currentSpeaker && currentText.length > 0) {
        groupedSegments.push({
          speakerName: currentSpeaker,
          speaker_label: currentSpeakerLabel!, // Original ID for color mapping
          text: currentText.join(' '),
          startTime: currentStartTime!,
          endTime: currentEndTime!,
          rawStartIndex: currentRawStartIndex,
          rawSegmentCount: currentRawCount,
          overlapGroupId: currentOverlapGroupId || undefined,
        });
      }
      currentSpeaker = speakerName;
      currentSpeakerLabel = speakerLabel; // Store original speaker ID (e.g., "SPEAKER_01")
      currentText = [segment.text];
      currentStartTime = startTime;
      currentEndTime = endTime;
      currentOverlapGroupId = overlapGroupId;
      currentRawStartIndex = rawIndex;
      currentRawCount = 1;
    } else {
      currentText.push(segment.text);
      currentEndTime = endTime; // Update end time to last segment
      currentRawCount++;
    }
  });

  // Add the last speaker block
  if (currentSpeaker && currentText.length > 0) {
    groupedSegments.push({
      speakerName: currentSpeaker,
      speaker_label: currentSpeakerLabel!, // Preserve for color mapping
      text: currentText.join(' '),
      startTime: currentStartTime!,
      endTime: currentEndTime!,
      rawStartIndex: currentRawStartIndex,
      rawSegmentCount: currentRawCount,
      overlapGroupId: currentOverlapGroupId || undefined,
    });
  }

  // Post-process: Group consecutive segments with the same overlapGroupId into overlap groups
  const finalSegments: ProcessedSegment[] = [];
  let i = 0;

  while (i < groupedSegments.length) {
    const segment = groupedSegments[i];

    if (segment.overlapGroupId) {
      // Collect all segments with the same overlap group ID
      const overlapGroup: ProcessedSegment[] = [segment];
      let j = i + 1;

      while (
        j < groupedSegments.length &&
        groupedSegments[j].overlapGroupId === segment.overlapGroupId
      ) {
        overlapGroup.push(groupedSegments[j]);
        j++;
      }

      if (overlapGroup.length > 1) {
        // Create an overlap group container
        const groupStartTime = Math.min(...overlapGroup.map((s) => s.startTime));
        const groupEndTime = Math.max(...overlapGroup.map((s) => s.endTime));
        const totalRawCount = overlapGroup.reduce((sum, s) => sum + s.rawSegmentCount, 0);

        finalSegments.push({
          speakerName: `${overlapGroup.length} speakers overlapping`,
          speaker_label: 'OVERLAP',
          text: '', // Container doesn't have its own text
          startTime: groupStartTime,
          endTime: groupEndTime,
          rawStartIndex: overlapGroup[0].rawStartIndex,
          rawSegmentCount: totalRawCount,
          isOverlapGroup: true,
          overlapGroupId: segment.overlapGroupId,
          overlapSegments: overlapGroup,
        });

        i = j;
      } else {
        // Single segment with overlap flag, just add it
        finalSegments.push(segment);
        i++;
      }
    } else {
      finalSegments.push(segment);
      i++;
    }
  }

  return finalSegments;
});
