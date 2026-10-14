import { describe, it, expect } from 'vitest';
import { resolveGroupedSegments } from './resolveGroupedSegments';
import type { GroupedTranscriptSegment } from '$lib/types/media';

function group(overrides: Partial<GroupedTranscriptSegment> = {}): GroupedTranscriptSegment {
  return {
    is_overlap_group: false,
    overlap_group_id: null,
    start_time: 0,
    end_time: 1,
    start_segment_index: 0,
    segment_uuids: [],
    ...overrides,
  };
}

describe('resolveGroupedSegments', () => {
  it('resolves a single-segment group by uuid', () => {
    const segments = [{ uuid: 'a', text: 'hello' }];
    const groups = resolveGroupedSegments(segments, [group({ segment_uuids: ['a'] })]);

    expect(groups).toHaveLength(1);
    expect(groups[0].segments).toEqual([{ uuid: 'a', text: 'hello' }]);
    expect(groups[0].isOverlapGroup).toBe(false);
  });

  it('drops a group that resolves to nothing (all referenced segments not loaded yet)', () => {
    const groups = resolveGroupedSegments([], [group({ segment_uuids: ['missing'] })]);
    expect(groups).toEqual([]);
  });

  it('skips references to segments that are not (yet) loaded, keeping the ones that are', () => {
    const segments = [{ uuid: 'a', text: 'hi' }];
    const groups = resolveGroupedSegments(segments, [
      group({ segment_uuids: ['a', 'not-loaded'], is_overlap_group: true }),
    ]);

    expect(groups).toHaveLength(1);
    // A run reduced to one member by the "not loaded" skip is no longer an overlap cluster.
    expect(groups[0].isOverlapGroup).toBe(false);
    expect(groups[0].segments).toEqual([{ uuid: 'a', text: 'hi' }]);
  });

  it('enforces that a segment belongs to exactly one group (uuid claimed by the first group wins)', () => {
    const segments = [{ uuid: 'a', text: 'hi' }];
    const groups = resolveGroupedSegments(segments, [
      group({ segment_uuids: ['a'] }),
      group({ segment_uuids: ['a'] }),
    ]);

    expect(groups).toHaveLength(1);
  });

  it('preserves a genuine multi-segment overlap group', () => {
    const segments = [
      { uuid: 'a', text: 'one' },
      { uuid: 'b', text: 'two' },
    ];
    const groups = resolveGroupedSegments(segments, [
      group({ segment_uuids: ['a', 'b'], is_overlap_group: true, overlap_group_id: 'og-1' }),
    ]);

    expect(groups[0].isOverlapGroup).toBe(true);
    expect(groups[0].overlapGroupId).toBe('og-1');
    expect(groups[0].segments).toHaveLength(2);
  });

  it('returns an empty array for no groups', () => {
    expect(resolveGroupedSegments([{ uuid: 'a' }], undefined)).toEqual([]);
    expect(resolveGroupedSegments([{ uuid: 'a' }], null)).toEqual([]);
  });
});
