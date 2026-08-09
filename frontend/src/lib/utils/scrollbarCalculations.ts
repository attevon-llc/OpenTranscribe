/**
 * Utility functions for calculating scrollbar position indicator placement
 * Handles edge cases and provides robust position calculations for transcript playhead tracking
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
 * Calculate scrollbar position based on current time relative to transcript timeline
 * This provides smooth movement that follows the video playhead exactly
 */
export function calculateScrollbarPositionBySegment(
  currentTime: number,
  transcriptSegments: TranscriptSegment[]
): number {
  if (
    !transcriptSegments ||
    transcriptSegments.length === 0 ||
    isNaN(currentTime) ||
    currentTime < 0
  ) {
    return 0;
  }

  // Sort segments by start time to ensure proper order
  const sortedSegments = [...transcriptSegments].sort((a, b) => a.start_time - b.start_time);

  // Get time bounds
  const firstSegment = sortedSegments[0];
  const lastSegment = sortedSegments[sortedSegments.length - 1];

  if (!firstSegment || !lastSegment) {
    return 0;
  }

  const totalStartTime = firstSegment.start_time;
  const totalEndTime = lastSegment.end_time;
  const totalDuration = totalEndTime - totalStartTime;

  if (totalDuration <= 0) {
    return 0;
  }

  // Calculate position based on time progression through the entire transcript
  // This ensures smooth movement that follows the video playhead exactly
  if (currentTime <= totalStartTime) {
    return 0;
  }

  if (currentTime >= totalEndTime) {
    return 100;
  }

  // Linear interpolation based on time position within the transcript
  const timeProgress = (currentTime - totalStartTime) / totalDuration;
  const position = timeProgress * 100;

  return Math.max(0, Math.min(100, position));
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

/**
 * Throttle function to limit the frequency of position updates
 * Prevents excessive DOM updates during playback
 */
export function createThrottledPositionUpdate(
  callback: (position: number) => void,
  delay: number = 16 // ~60fps
): (position: number) => void {
  let lastCallTime = 0;
  let animationFrameId: number | null = null;

  return (position: number) => {
    const now = Date.now();

    if (now - lastCallTime >= delay) {
      lastCallTime = now;
      callback(position);
    } else {
      // Schedule update for next frame if not already scheduled
      if (animationFrameId === null) {
        animationFrameId = requestAnimationFrame(() => {
          callback(position);
          lastCallTime = Date.now();
          animationFrameId = null;
        });
      }
    }
  };
}
