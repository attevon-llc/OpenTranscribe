/**
 * Pure speaker-name rules for the file-detail page: validation, change detection against
 * the last-saved baseline, which renames need a profile decision, and what a bulk save
 * writes back. No I/O; `speakerLoader` and `speakerSave` hold the request flows.
 */
import type { Translate } from '$lib/i18n';
import type { SpeakerProfileSuggestion } from '$lib/types/speaker';
import type { SpeakerInfo } from '$stores/transcriptStore';

/** A speaker as the page holds it: the backend row plus UI-only flags. */
export interface SpeakerItem extends SpeakerInfo {
  profile?: { uuid: string; name: string } | null;
  profile_suggestions?: SpeakerProfileSuggestion[];
  [key: string]: unknown;
}

/** Last-saved display names by speaker uuid, trimmed. */
export type SpeakerNameBaseline = Map<string, string>;

/** The speaker decision a linked profile needs before a rename is saved. */
export type ProfileDecision = 'update_profile' | 'create_new_profile';

export function validateSpeakerName(
  name: string,
  t: Translate
): { isValid: boolean; error?: string } {
  if (!name || typeof name !== 'string') {
    return { isValid: false, error: t('speakerValidation.nameRequired') };
  }

  const trimmedName = name.trim();
  if (trimmedName.length === 0) {
    return { isValid: false, error: t('speakerValidation.nameEmpty') };
  }

  if (trimmedName.length > 100) {
    return { isValid: false, error: t('speakerValidation.nameTooLong') };
  }

  // Allow duplicate display names - users can label multiple speakers with the same name
  // and merge them later using the Speaker Merge feature when confident they're the same person
  return { isValid: true };
}

/**
 * Whether this speaker's display name differs from the last-saved baseline —
 * including a change TO empty, which is the supported way to clear a label back
 * to the raw diarizer name (`PUT /speakers/{uuid}` accepts `display_name: ""`;
 * `canonical_speaker_label` on the backend falls back to the raw `SPEAKER_XX`
 * name whenever `display_name` is empty/unset). Filtering on the CURRENT
 * value's shape — empty or `SPEAKER_`-prefixed — instead of on whether it
 * actually changed was the bug: it silently dropped exactly the "clear a
 * label" case from the save payload, while the bulk save still reported
 * "saved successfully" for it.
 */
export function speakerHasUnsavedChange(
  speaker: SpeakerItem,
  baseline: SpeakerNameBaseline
): boolean {
  if (!speaker.uuid) return false;
  const originalName = baseline.get(speaker.uuid) ?? '';
  const currentName = (speaker.display_name ?? '').trim();
  return originalName !== currentName;
}

/** Drives the "unsaved" indicator and the leave-page guard (issue #787). */
export function hasUnsavedSpeakerNames(
  speakers: SpeakerItem[],
  baseline: SpeakerNameBaseline
): boolean {
  return (
    speakers.length > 0 &&
    speakers.some((speaker) => {
      const originalName = baseline.get(speaker.uuid) || '';
      const currentName = (speaker.display_name || '').trim();
      return originalName !== currentName;
    })
  );
}

/** The baseline for a list: each speaker's current display name, trimmed. */
export function baselineFrom(
  speakers: Array<{ uuid: string; display_name?: string }>
): SpeakerNameBaseline {
  return new Map(speakers.map((speaker) => [speaker.uuid, (speaker.display_name || '').trim()]));
}

/**
 * Unsaved names worth carrying across a reload of the list: a websocket event or a sibling
 * rename can land while the user is typing in another speaker's field.
 */
export function unsavedNamesToKeep(
  speakers: SpeakerItem[],
  baseline: SpeakerNameBaseline
): Map<string, string> {
  const unsaved = new Map<string, string>();
  for (const speaker of speakers) {
    const saved = baseline.get(speaker.uuid);
    const current = (speaker.display_name || '').trim();
    if (saved !== undefined && saved !== current) {
      unsaved.set(speaker.uuid, speaker.display_name || '');
    }
  }
  return unsaved;
}

/**
 * Renames in a bulk save that need an update-globally / create-new decision: a linked
 * profile whose name differs from the new one. Never with diarization off, and never for a
 * clear (there is no new name to reconcile against the profile).
 */
export function speakersNeedingProfileDecision(
  speakers: SpeakerItem[],
  diarizationDisabled: boolean
): SpeakerItem[] {
  if (diarizationDisabled) return [];
  return speakers.filter(
    (speaker) =>
      speaker.profile &&
      (speaker.display_name ?? '').trim() !== '' &&
      speaker.profile.name !== (speaker.display_name ?? '').trim()
  );
}

/**
 * Optimistically rename profile suggestions after an "update profile globally" decision,
 * so every speaker's suggestion chips show the new profile name at once.
 *
 * Mutates each speaker's `profile_suggestions` in place, as the page always has: the speaker
 * editor holds references to these objects.
 */
export function renameProfileSuggestions(
  speakers: SpeakerItem[],
  nameChanges: Map<string, { oldName: string; newName: string }>
): SpeakerItem[] {
  if (nameChanges.size === 0) return speakers;
  return speakers.map((s) => {
    if (s.profile_suggestions && s.profile_suggestions.length > 0) {
      s.profile_suggestions = s.profile_suggestions.map((suggestion) => {
        for (const change of nameChanges.values()) {
          if (suggestion.name === change.oldName && suggestion.suggestion_type === 'profile') {
            return { ...suggestion, name: change.newName };
          }
        }
        return suggestion;
      });
    }
    return s;
  });
}

/**
 * The renames to paint after a bulk save, for every speaker that saved. A cleared speaker
 * (empty `display_name`) is painted with its raw `speaker.name` (SPEAKER_XX), never `''`:
 * `renameSpeakersInFile` / `transcriptStore.updateSpeakerName` write the string VERBATIM
 * into `resolved_speaker_name`, and an empty one renders as "Unknown Speaker".
 */
export function savedSpeakerRenames(
  saved: SpeakerItem[],
  failedUuids: Set<string>
): Array<{ uuid: string; label: string; displayName: string }> {
  return saved
    .filter((speaker) => speaker.uuid && !failedUuids.has(speaker.uuid))
    .map((speaker) => {
      const trimmed = (speaker.display_name ?? '').trim();
      return {
        uuid: speaker.uuid,
        label: speaker.name,
        displayName: trimmed === '' ? speaker.name ?? '' : trimmed,
      };
    });
}

/**
 * Fallback speaker list built from the segments when `GET /speakers` fails or returns
 * something that is not a list: one entry per distinct label, first-seen order.
 */
export function speakersFromSegments(
  segments: Array<{
    speaker_label?: string | null;
    speaker?: { name?: string; display_name?: string } | null;
  }>,
  unknownLabel: string
): SpeakerItem[] {
  const speakers = new Map<string, { name: string; display_name: string }>();
  segments.forEach((segment) => {
    const speakerLabel = segment.speaker_label || segment.speaker?.name || unknownLabel;
    if (!speakers.has(speakerLabel)) {
      speakers.set(speakerLabel, {
        name: speakerLabel,
        display_name: segment.speaker?.display_name || speakerLabel,
      });
    }
  });
  return Array.from(speakers.values()) as unknown as SpeakerItem[];
}
