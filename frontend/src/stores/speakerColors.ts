/**
 * Speaker Color Store
 *
 * Reactive Svelte store that maintains consistent speaker-to-color mappings
 * across all components. This ensures that SPEAKER_01, SPEAKER_02, etc.
 * always have the same colors regardless of user-assigned labels.
 */

import { writable, get } from 'svelte/store';
import { getSpeakerColor } from '$lib/utils/speakerColors';

interface SpeakerColorMapping {
  [speakerId: string]: {
    bg: string;
    border: string;
    textLight: string;
    textDark: string;
  };
}

// Create the reactive store
const speakerColorMappings = writable<SpeakerColorMapping>({});

/**
 * Get consistent color for a speaker by their original ID
 * This function ensures colors are cached and consistent
 */
export function getSpeakerColorFromStore(speakerId: string) {
  // Get current mappings using get() to avoid memory leak
  const currentMappings = get(speakerColorMappings);

  // If we already have a color for this speaker, return it
  if (currentMappings[speakerId]) {
    return currentMappings[speakerId];
  }

  // Generate new color using the utility function
  const color = getSpeakerColor(speakerId);

  // Store the color for future consistency
  speakerColorMappings.update((mappings) => ({
    ...mappings,
    [speakerId]: color,
  }));

  return color;
}

/**
 * Clear all speaker color mappings (useful for testing or reset)
 */
export function clearSpeakerColorMappings() {
  speakerColorMappings.set({});
}

/**
 * Get the reactive store for components that need to subscribe to changes
 */
export { speakerColorMappings };

/**
 * Various object shapes a speaker identifier can arrive in: a raw string, a
 * transcript segment (`speaker_label`), a speaker object (`name`), or a wrapper
 * with a nested `speaker`.
 */
type SpeakerColorSource =
  | string
  | {
      speaker_label?: string;
      name?: string;
      speaker?: { name?: string };
    }
  | null
  | undefined;

/**
 * Helper function to get speaker color from various data sources
 * Tries to find the original speaker ID from different object structures
 */
export function getSpeakerColorSmart(speakerData: SpeakerColorSource) {
  const obj = typeof speakerData === 'object' && speakerData !== null ? speakerData : undefined;
  const asString = typeof speakerData === 'string' ? speakerData : undefined;
  // Try different ways to get the original speaker ID. Deliberately `||`, not `??`:
  // an empty-string candidate is treated the same as a missing one and falls through
  // to the next source (or ultimately 'Unknown'), matching the equivalent inline
  // `segment.speaker_label || segment.speaker?.name || ...` chains used throughout
  // the transcript components (VideoPlayer.svelte, TranscriptSegmentList.svelte,
  // SegmentSpeakerDropdown.svelte). Backend `speaker_label`/`name` are `str | None`
  // with no evidence of an empty-string value in practice, so this stays a
  // documented, pinned behavior rather than a speculative `??` change.
  const speakerId =
    obj?.speaker_label || // For transcript segments (now contains original ID)
    obj?.name || // For speaker objects
    obj?.speaker?.name || // For nested speaker objects
    asString || // If speakerData is just a string
    'Unknown';

  return getSpeakerColorFromStore(speakerId);
}
