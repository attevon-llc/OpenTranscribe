/**
 * Pure rules behind the per-segment speaker menu (`SegmentSpeakerDropdown.svelte`):
 * what the trigger shows, whether a typed name may be saved, which profile names to
 * suggest while typing, and how the speaker list is ordered and labelled.
 *
 * Kept free of the DOM and of the i18n store so each rule is testable on its own; the
 * label translation is passed in.
 */
import type { Speaker } from '$lib/types/speaker';
import type { TranscriptSegment } from '$lib/types/media';
import { isPlaceholderSpeakerName, placeholderSpeakerNumber } from '$lib/utils/speakerNames';

export type WireSpeaker = NonNullable<TranscriptSegment['speaker']>;

export type InlineNameCheck = 'ok' | 'empty' | 'placeholder' | 'unchanged';

/** How many saved profile names the inline field offers while typing. */
export const MAX_PROFILE_MATCHES = 5;

export interface PendingSuggestion {
  uuid: string;
  name: string;
  confidence: number;
}

/**
 * `display_name` is the ONLY field that means "a human confirmed this name":
 * `POST /speakers` and `PUT /speakers/{uuid}` both flip `verified` the moment it is set.
 *
 * Deliberately NOT part of this chain: `speaker.resolved_display_name` and
 * `segment.resolved_speaker_name`. Both are `canonical_speaker_label(...)`, which returns a
 * voiceprint-matched `suggested_name` (>= 0.75) as well as a typed one, so they cover
 * "confirmed" and "matched by voice" in one string (#741).
 */
export function confirmedSpeakerName(wireSpeaker: WireSpeaker | null): string {
  return (wireSpeaker?.display_name ?? '').trim();
}

/** The diarization slot or segment label shown when no human name exists. */
export function speakerNumberLabelOf(
  wireSpeaker: WireSpeaker | null,
  segmentLabel: string | undefined
): string {
  return wireSpeaker?.name || segmentLabel || '';
}

/**
 * An unconfirmed suggestion is surfaced as its own menu row, with the confidence score, never
 * as the speaker's name. Returns `null` once a human has confirmed a name.
 */
export function pendingSuggestionOf(
  wireSpeaker: WireSpeaker | null,
  confirmedName: string
): PendingSuggestion | null {
  return !confirmedName && wireSpeaker?.suggested_name && wireSpeaker.uuid
    ? {
        uuid: wireSpeaker.uuid,
        name: wireSpeaker.suggested_name,
        confidence: wireSpeaker.confidence ?? 0,
      }
    : null;
}

/** Whether a name typed into the inline field may be saved for this speaker. */
export function inlineNameCheck(raw: string, confirmedName: string): InlineNameCheck {
  const name = raw.trim();
  if (name === '') return 'empty';
  // `SPEAKER_NN` is a diarization slot, not an identity -- same rule as "Add speaker".
  if (isPlaceholderSpeakerName(name)) return 'placeholder';
  if (name === confirmedName) return 'unchanged';
  return 'ok';
}

/** Distinct, non-empty profile names, in the order the API returned them. */
export function uniqueProfileNames(profiles: { name: string }[] | null | undefined): string[] {
  return [...new Set((profiles ?? []).map((p) => p.name).filter(Boolean))];
}

/** Saved profile names containing what was typed, alphabetical and capped. */
export function matchingProfileNames(profileNames: string[], rawTyped: string): string[] {
  const typed = rawTyped.trim().toLowerCase();
  return typed === ''
    ? []
    : profileNames
        .filter((name) => name.toLowerCase().includes(typed))
        // The API's order is unspecified; a stable one keeps the list from shuffling.
        .sort((a, b) => a.localeCompare(b))
        .slice(0, MAX_PROFILE_MATCHES);
}

/**
 * Human-named speakers first, then unidentified `SPEAKER_##` slots. Classification uses the
 * effective display name, so a speaker renamed to "John Smith" (display_name) is treated as
 * named even though the internal name is still "SPEAKER_01".
 */
export function partitionSpeakers(speakers: Speaker[]): { named: Speaker[]; auto: Speaker[] } {
  const isAutoLabel = (s: Speaker) => isPlaceholderSpeakerName(s.display_name || s.name);
  return {
    named: speakers.filter((s) => !isAutoLabel(s)),
    auto: speakers.filter((s) => isAutoLabel(s)),
  };
}

/**
 * The menu row label. If the speaker has a human name but the underlying id is `SPEAKER_##`,
 * the slot number is appended so the user can see continuity in the ordered list.
 */
export function speakerMenuLabel(speaker: Speaker, translate: (name: string) => string): string {
  const slotNumber = placeholderSpeakerNumber(speaker.name);
  const effectiveName = translate(speaker.display_name || speaker.name);
  return slotNumber !== null && speaker.display_name
    ? `${effectiveName} (${slotNumber.toString().padStart(2, '0')})`
    : effectiveName;
}

/**
 * Whether the "Add speaker" dialog may create. A typed name that is itself `SPEAKER_NN` is a
 * placeholder, not an identity: accepting it would recreate exactly the state #740 exists to
 * stop (a bare slot with no human name).
 */
export function newSpeakerNameState(
  name: string,
  isCreating: boolean
): { isPlaceholder: boolean; canCreate: boolean } {
  const typed = name.trim() !== '';
  const isPlaceholder = typed && isPlaceholderSpeakerName(name);
  return { isPlaceholder, canCreate: !isCreating && typed && !isPlaceholder };
}
