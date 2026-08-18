/**
 * `speakerColors.ts` keeps SPEAKER_01/SPEAKER_02/etc. mapped to a consistent color
 * across every component that renders them, by caching the result of
 * `getSpeakerColor()` (from `$lib/utils/speakerColors`) the first time a given
 * speaker id is seen. These tests pin the caching behavior itself (never call the
 * underlying generator twice for the same id) and `getSpeakerColorSmart`'s full
 * candidate fallback chain — including BC-29: an empty-string candidate at any
 * position in the chain is treated as absent and falls through to the next
 * source, exactly like the equivalent inline `||` chains used in
 * VideoPlayer.svelte / TranscriptSegmentList.svelte / SegmentSpeakerDropdown.svelte.
 * That is a deliberate, pinned decision, not an oversight — see the comment above
 * `getSpeakerColorSmart` in the source file.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { get } from 'svelte/store';

const mockGetSpeakerColor = vi.hoisted(() => vi.fn());
vi.mock('$lib/utils/speakerColors', () => ({
  getSpeakerColor: mockGetSpeakerColor,
}));

import {
  getSpeakerColorFromStore,
  clearSpeakerColorMappings,
  getSpeakerColorSmart,
  speakerColorMappings,
} from './speakerColors';

function colorFor(id: string) {
  return {
    bg: `bg-${id}`,
    border: `border-${id}`,
    textLight: `light-${id}`,
    textDark: `dark-${id}`,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetSpeakerColor.mockImplementation((id: string) => colorFor(id));
  clearSpeakerColorMappings();
});

describe('getSpeakerColorFromStore', () => {
  it('generates a color for an unseen speaker and stores it in the reactive store', () => {
    const color = getSpeakerColorFromStore('SPEAKER_01');

    expect(color).toEqual(colorFor('SPEAKER_01'));
    expect(mockGetSpeakerColor).toHaveBeenCalledTimes(1);
    expect(mockGetSpeakerColor).toHaveBeenCalledWith('SPEAKER_01');
    expect(get(speakerColorMappings)).toEqual({ SPEAKER_01: colorFor('SPEAKER_01') });
  });

  it('returns the cached color on a second call without regenerating it', () => {
    const first = getSpeakerColorFromStore('SPEAKER_01');
    const second = getSpeakerColorFromStore('SPEAKER_01');

    expect(second).toEqual(first);
    // The whole point of the store: the generator is consulted once per id, ever.
    expect(mockGetSpeakerColor).toHaveBeenCalledTimes(1);
  });

  it('caches distinct speakers independently', () => {
    getSpeakerColorFromStore('SPEAKER_01');
    getSpeakerColorFromStore('SPEAKER_02');
    getSpeakerColorFromStore('SPEAKER_01');

    expect(mockGetSpeakerColor).toHaveBeenCalledTimes(2);
    expect(get(speakerColorMappings)).toEqual({
      SPEAKER_01: colorFor('SPEAKER_01'),
      SPEAKER_02: colorFor('SPEAKER_02'),
    });
  });
});

describe('clearSpeakerColorMappings', () => {
  it('empties the store so a previously-seen speaker is regenerated', () => {
    getSpeakerColorFromStore('SPEAKER_01');
    expect(get(speakerColorMappings)).not.toEqual({});

    clearSpeakerColorMappings();

    expect(get(speakerColorMappings)).toEqual({});

    getSpeakerColorFromStore('SPEAKER_01');
    // One call before clearing, one call after — proves the cache entry was
    // actually dropped rather than clear() being a no-op on the read path.
    expect(mockGetSpeakerColor).toHaveBeenCalledTimes(2);
  });
});

describe('getSpeakerColorSmart', () => {
  it.each([
    ['a plain string', 'SPEAKER_07', 'SPEAKER_07'],
    ['an object with only speaker_label', { speaker_label: 'SPEAKER_01' }, 'SPEAKER_01'],
    ['an object with only name', { name: 'Alice' }, 'Alice'],
    ['an object with only a nested speaker.name', { speaker: { name: 'Bob' } }, 'Bob'],
    ['null', null, 'Unknown'],
    ['undefined', undefined, 'Unknown'],
    ['an empty object', {}, 'Unknown'],
  ])('resolves %s to speaker id %s', (_desc, input, expectedId) => {
    const color = getSpeakerColorSmart(input as never);

    expect(mockGetSpeakerColor).toHaveBeenCalledWith(expectedId);
    expect(color).toEqual(colorFor(expectedId));
  });

  it('prefers speaker_label over name and nested speaker.name when all are present', () => {
    const color = getSpeakerColorSmart({
      speaker_label: 'SPEAKER_01',
      name: 'Alice',
      speaker: { name: 'Bob' },
    });

    expect(mockGetSpeakerColor).toHaveBeenCalledWith('SPEAKER_01');
    expect(color).toEqual(colorFor('SPEAKER_01'));
  });

  it('prefers name over nested speaker.name when speaker_label is absent', () => {
    const color = getSpeakerColorSmart({ name: 'Alice', speaker: { name: 'Bob' } });

    expect(mockGetSpeakerColor).toHaveBeenCalledWith('Alice');
    expect(color).toEqual(colorFor('Alice'));
  });

  // BC-29: `||`, not `??` — an empty-string candidate is treated as absent and the
  // chain falls through to the next source, rather than using the empty string as
  // the (technically legitimate) speaker id. Pinned as the current, deliberate
  // behavior; see the comment in speakerColors.ts above getSpeakerColorSmart.
  describe('BC-29: empty-string candidates fall through rather than being used as-is', () => {
    it.each([
      ['empty speaker_label falls through to name', { speaker_label: '', name: 'Alice' }, 'Alice'],
      [
        'empty speaker_label AND name falls through to nested speaker.name',
        { speaker_label: '', name: '', speaker: { name: 'Bob' } },
        'Bob',
      ],
      [
        'empty nested speaker.name falls through to Unknown when nothing else is present',
        { speaker: { name: '' } },
        'Unknown',
      ],
      ['an empty string input falls through to Unknown', '', 'Unknown'],
      [
        'every candidate empty falls all the way through to Unknown',
        { speaker_label: '', name: '', speaker: { name: '' } },
        'Unknown',
      ],
    ])('%s', (_desc, input, expectedId) => {
      const color = getSpeakerColorSmart(input as never);

      expect(mockGetSpeakerColor).toHaveBeenCalledWith(expectedId);
      expect(color).toEqual(colorFor(expectedId));
    });
  });

  it('returns the color produced for the resolved speaker id, through the caching store', () => {
    const color = getSpeakerColorSmart({ speaker_label: 'SPEAKER_09' });

    expect(color).toEqual(colorFor('SPEAKER_09'));
    expect(get(speakerColorMappings).SPEAKER_09).toEqual(colorFor('SPEAKER_09'));
  });
});
