/**
 * The pure parts of reassigning one transcript segment to another speaker: the optimistic
 * patch, the rollback patch, and the check for a speaker the reassignment left without
 * segments. The component owns the request and the page state.
 */
import type { TranscriptSegment } from '$lib/types/media';
import type { Speaker } from '$lib/types/speaker';

/**
 * The speaker an assignment shows before the server answers.
 *
 * `createdSpeaker` is the just-created row handed over by the dropdown's "Add speaker" flow.
 * `speakerList` is reloaded asynchronously by the route, so it CANNOT contain a speaker
 * created a millisecond ago — looking only there rendered the segment "Unknown", which made
 * "Add speaker" look like it had done nothing at all (#740).
 */
export function pickNewSpeaker(
  speakerUuid: string | null | undefined,
  speakerList: Speaker[],
  createdSpeaker: Speaker | null | undefined
): Speaker | null {
  if (!speakerUuid) return null;
  return speakerList.find((s) => s.uuid === speakerUuid) ?? createdSpeaker ?? null;
}

/** The segment fields an optimistic assignment writes. */
export function assignmentPatch(speaker: Speaker | null) {
  return {
    speaker,
    speaker_id: speaker?.uuid ?? null,
    resolved_speaker_name: speaker?.display_name || speaker?.name || null,
  };
}

/** The segment fields that put a failed assignment back as it was. */
export function rollbackPatch(
  originalSpeaker: Speaker | null | undefined,
  originalResolvedName: string | null | undefined
) {
  return {
    speaker: originalSpeaker,
    speaker_id: originalSpeaker?.uuid ?? null,
    resolved_speaker_name: originalResolvedName ?? null,
  };
}

/**
 * The uuid of `originalSpeaker` when no segment references it any more. The backend deletes an
 * orphaned speaker, so the page must drop it from its list too.
 */
export function orphanedSpeakerUuid(
  segments: TranscriptSegment[],
  originalSpeaker: Speaker | null | undefined
): string | null {
  const uuid = originalSpeaker?.uuid;
  if (!uuid) return null;
  return segments.some((s) => s.speaker?.uuid === uuid) ? null : uuid;
}
