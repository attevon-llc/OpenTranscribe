/**
 * Utility functions for locating the transcript segment under the current playhead time.
 *
 * `calculateScrollbarPositionBySegment` and `createThrottledPositionUpdate` were deleted
 * with `ScrollbarIndicator.svelte` (issue #748 §5.3) — that minimap component was their only
 * consumer. `findCurrentSegment` survives: it is still needed to locate the target segment
 * for the "Jump to current" button that replaced the indicator
 * (`TranscriptDisplay.handleJumpToPlayhead`).
 */

export interface TranscriptSegment {
  uuid?: string;
  id?: string | number;
  start_time: number;
  end_time: number;
  text: string;
  speaker_label?: string;
  speaker?: {
    uuid?: string;
    name?: string;
    display_name?: string;
  };
}

/**
 * Find the segment that contains the current playback time
 * Returns null if no segment contains the current time
 */
export function findCurrentSegment(
  currentTime: number,
  transcriptSegments: TranscriptSegment[]
): TranscriptSegment | null {
  if (!transcriptSegments || transcriptSegments.length === 0 || isNaN(currentTime)) {
    return null;
  }

  // Find segment containing current time with tolerance for floating point precision
  const tolerance = 0.1; // 100ms tolerance

  for (const segment of transcriptSegments) {
    if (
      typeof segment.start_time === 'number' &&
      typeof segment.end_time === 'number' &&
      currentTime >= segment.start_time - tolerance &&
      currentTime <= segment.end_time + tolerance
    ) {
      return segment;
    }
  }

  return null;
}
