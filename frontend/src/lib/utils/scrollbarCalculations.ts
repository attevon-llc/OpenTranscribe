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
 * Calculate the playhead position relative to the visible scrollbar area
 * This positions the indicator based on where in the scrollable content the current segment appears
 */
export function calculateScrollbarPosition(
  currentTime: number,
  transcriptSegments: TranscriptSegment[]
): number {
  // Edge case: No segments or invalid current time
  if (
    !transcriptSegments ||
    transcriptSegments.length === 0 ||
    isNaN(currentTime) ||
    currentTime < 0
  ) {
    return 0;
  }

  // Edge case: Single segment
  if (transcriptSegments.length === 1) {
    const segment = transcriptSegments[0];
    if (currentTime <= segment.start_time) return 0;
    if (currentTime >= segment.end_time) return 100;

    // For single segment, calculate position within that segment
    const segmentDuration = segment.end_time - segment.start_time;
    if (segmentDuration <= 0) return 0;

    const progressInSegment = (currentTime - segment.start_time) / segmentDuration;
    return Math.max(0, Math.min(100, progressInSegment * 100));
  }

  // Find the earliest start time and latest end time
  let earliestStart = Infinity;
  let latestEnd = -Infinity;

  for (const segment of transcriptSegments) {
    if (typeof segment.start_time === 'number' && !isNaN(segment.start_time)) {
      earliestStart = Math.min(earliestStart, segment.start_time);
    }
    if (typeof segment.end_time === 'number' && !isNaN(segment.end_time)) {
      latestEnd = Math.max(latestEnd, segment.end_time);
    }
  }

  // Edge case: Invalid time ranges
  if (earliestStart === Infinity || latestEnd === -Infinity || latestEnd <= earliestStart) {
    return 0;
  }

  const totalDuration = latestEnd - earliestStart;

  // Edge case: Zero duration
  if (totalDuration <= 0) {
    return 0;
  }

  // Normalize current time to transcript time range
  const normalizedTime = currentTime - earliestStart;

  // Edge case: Current time before transcript starts
  if (normalizedTime <= 0) {
    return 0;
  }

  // Edge case: Current time after transcript ends
  if (normalizedTime >= totalDuration) {
    return 100;
  }

  // Calculate position as percentage
  const position = (normalizedTime / totalDuration) * 100;

  // Ensure result is within valid range
  return Math.max(0, Math.min(100, position));
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
 * Calculate the scroll position needed to center a segment in the transcript view
 * Returns a percentage (0-100) of the total scrollable area
 */
export function calculateSegmentScrollPosition(
  targetSegment: TranscriptSegment,
  transcriptSegments: TranscriptSegment[],
  containerHeight: number = 600
): number {
  if (!targetSegment || !transcriptSegments || transcriptSegments.length === 0) {
    return 0;
  }

  // Find the index of the target segment
  const segmentIndex = transcriptSegments.findIndex(
    (segment) =>
      (segment.uuid || segment.id) === (targetSegment.uuid || targetSegment.id) ||
      (segment.start_time === targetSegment.start_time &&
        segment.end_time === targetSegment.end_time)
  );

  if (segmentIndex === -1) {
    return 0;
  }

  // Estimate segment height (could be refined with actual measurements)
  const estimatedSegmentHeight = 60; // Based on CSS analysis of .transcript-segment
  const totalEstimatedHeight = transcriptSegments.length * estimatedSegmentHeight;

  // If content is shorter than container, no scrolling needed
  if (totalEstimatedHeight <= containerHeight) {
    return 0;
  }

  // Calculate position to center the target segment
  const targetSegmentTop = segmentIndex * estimatedSegmentHeight;
  const centerOffset = containerHeight / 2 - estimatedSegmentHeight / 2;
  const scrollTop = Math.max(0, targetSegmentTop - centerOffset);

  // Convert to percentage
  const maxScrollTop = totalEstimatedHeight - containerHeight;
  const scrollPercentage = maxScrollTop > 0 ? (scrollTop / maxScrollTop) * 100 : 0;

  return Math.max(0, Math.min(100, scrollPercentage));
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

/**
 * Calculate time from a click position on the scrollbar
 * Converts scrollbar percentage back to transcript time
 */
export function calculateTimeFromScrollbarPosition(
  clickPosition: number, // 0-100 percentage
  transcriptSegments: TranscriptSegment[]
): number {
  if (
    !transcriptSegments ||
    transcriptSegments.length === 0 ||
    clickPosition < 0 ||
    clickPosition > 100
  ) {
    return 0;
  }

  // Find time range
  let earliestStart = Infinity;
  let latestEnd = -Infinity;

  for (const segment of transcriptSegments) {
    if (typeof segment.start_time === 'number' && !isNaN(segment.start_time)) {
      earliestStart = Math.min(earliestStart, segment.start_time);
    }
    if (typeof segment.end_time === 'number' && !isNaN(segment.end_time)) {
      latestEnd = Math.max(latestEnd, segment.end_time);
    }
  }

  if (earliestStart === Infinity || latestEnd === -Infinity || latestEnd <= earliestStart) {
    return 0;
  }

  const totalDuration = latestEnd - earliestStart;
  const targetTime = earliestStart + (clickPosition / 100) * totalDuration;

  return Math.max(earliestStart, Math.min(latestEnd, targetTime));
}

/**
 * Validate transcript segments array for common issues
 * Returns validation result with error details
 */
export function validateTranscriptSegments(segments: unknown): {
  isValid: boolean;
  errors: string[];
  warnings: string[];
} {
  const errors: string[] = [];
  const warnings: string[] = [];

  if (!Array.isArray(segments)) {
    errors.push('Transcript segments must be an array');
    return { isValid: false, errors, warnings };
  }

  if (segments.length === 0) {
    warnings.push('Transcript segments array is empty');
    return { isValid: true, errors, warnings };
  }

  for (let i = 0; i < segments.length; i++) {
    const raw = segments[i];

    if (!raw || typeof raw !== 'object') {
      errors.push(`Segment ${i} is not a valid object`);
      continue;
    }

    const segment = raw as { start_time?: unknown; end_time?: unknown; text?: unknown };

    if (typeof segment.start_time !== 'number' || isNaN(segment.start_time)) {
      errors.push(`Segment ${i} has invalid start_time`);
    }

    if (typeof segment.end_time !== 'number' || isNaN(segment.end_time)) {
      errors.push(`Segment ${i} has invalid end_time`);
    }

    if (
      typeof segment.start_time === 'number' &&
      typeof segment.end_time === 'number' &&
      segment.start_time >= segment.end_time
    ) {
      warnings.push(`Segment ${i} has start_time >= end_time`);
    }

    if (typeof segment.text !== 'string' || segment.text.trim() === '') {
      warnings.push(`Segment ${i} has empty or invalid text`);
    }
  }

  return { isValid: errors.length === 0, errors, warnings };
}
