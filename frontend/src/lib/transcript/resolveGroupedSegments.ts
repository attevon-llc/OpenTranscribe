/**
 * Resolve the backend's uuid-referencing `grouped_segments` against a flat
 * `transcript_segments` list into the camelCase `GroupedSegmentView[]` the renderer consumes.
 *
 * Extracted from `TranscriptDisplay.mapBackendGroup` (issue #755) so the search-result "view
 * transcript" surface (`TranscriptViewModal`, self-fetching per its own paging) can share the
 * exact same resolution — including the single-representation invariant (#352) and the
 * "a group can reference a not-yet-loaded segment" skip — rather than growing a second copy.
 */

import type { GroupedSegmentView, GroupedTranscriptSegment } from '$lib/types/media';

interface SegmentLike {
  uuid?: string | number | null;
}

export function resolveGroupedSegments<T extends SegmentLike>(
  transcriptSegments: T[],
  groupedSegmentsRaw: GroupedTranscriptSegment[] | undefined | null
): GroupedSegmentView[] {
  const segmentsByUuid = new Map<string, T>();
  for (const segment of transcriptSegments || []) {
    if (segment?.uuid != null) segmentsByUuid.set(String(segment.uuid), segment);
  }

  const claimed = new Set<string>();

  function mapGroup(group: GroupedTranscriptSegment): GroupedSegmentView {
    const segments: T[] = [];
    for (const raw of group.segment_uuids || []) {
      const uuid = String(raw);
      if (claimed.has(uuid)) continue;
      const segment = segmentsByUuid.get(uuid);
      if (!segment) continue;
      claimed.add(uuid);
      segments.push(segment);
    }
    return {
      isOverlapGroup: (group.is_overlap_group ?? false) && segments.length > 1,
      overlapGroupId: group.overlap_group_id ?? undefined,
      startTime: group.start_time,
      endTime: group.end_time,
      segments,
      startSegmentIndex: group.start_segment_index ?? 0,
    };
  }

  return (groupedSegmentsRaw || []).map(mapGroup).filter((group) => group.segments.length > 0);
}
