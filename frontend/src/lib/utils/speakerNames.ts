/**
 * Placeholder-vs-human speaker names — the single frontend home for the rule.
 *
 * THE CONTRACT (shared with the backend's `^SPEAKER_\d+$` predicate in
 * `backend/app/api/endpoints/speakers.py` and with `canonical_speaker_label`):
 *
 * - A name matching `^SPEAKER_\d+$` is a **diarization placeholder**, not a
 *   human-assigned identity. So is `null` / `undefined` / an empty-or-blank string.
 * - Anything else is a **human-assigned name**.
 * - A placeholder is scoped to ONE FILE. `SPEAKER_00` in file A and `SPEAKER_00`
 *   in file B are different people — never treat a placeholder as a cross-file
 *   identity (no merging, matching or faceting on it).
 *
 * This module exists because the rule was open-coded in at least three places with
 * three slightly different spellings — `/^SPEAKER_\d+$/.test(...)`, a
 * `.startsWith('SPEAKER_')` variant that also swallowed a real name like
 * `SPEAKER_OF_THE_HOUSE`, and the generator that mints the labels — so a fix to one
 * never reached the others. Call these functions; do not re-derive the regex.
 */

/** The one spelling of "this is a diarization placeholder". */
const PLACEHOLDER_SPEAKER_NAME_PATTERN = /^SPEAKER_(\d+)$/;

/**
 * Is this a diarization placeholder rather than a human-assigned name?
 *
 * Blank/absent counts as a placeholder: an unnamed speaker has no more human
 * identity than `SPEAKER_00` does.
 */
export function isPlaceholderSpeakerName(name: string | null | undefined): boolean {
  const trimmed = (name ?? '').trim();
  if (trimmed === '') return true;
  return PLACEHOLDER_SPEAKER_NAME_PATTERN.test(trimmed);
}

/**
 * The numeric index of a placeholder name, or `null` when it isn't one.
 *
 * Note this returns `null` for a blank name even though
 * {@link isPlaceholderSpeakerName} calls blank a placeholder — a blank name has no
 * number to report.
 */
export function placeholderSpeakerNumber(name: string | null | undefined): number | null {
  const match = (name ?? '').trim().match(PLACEHOLDER_SPEAKER_NAME_PATTERN);
  return match ? parseInt(match[1], 10) : null;
}

/**
 * The next free placeholder slot for a file, e.g. `SPEAKER_03` when `SPEAKER_00`,
 * `SPEAKER_01` and `SPEAKER_02` are taken.
 *
 * This is a **slot**, not a name: it belongs in a speaker's `name` column (speaker
 * colours hash it, and it keeps the file's numbering continuous). A speaker the user
 * creates by hand must ALSO carry a human `display_name` — minting a bare placeholder
 * with nothing else is issue #740's original bug.
 */
export function nextPlaceholderSpeakerName(names: (string | null | undefined)[]): string {
  let maxNumber = -1;
  for (const name of names) {
    const number = placeholderSpeakerNumber(name);
    if (number !== null && number > maxNumber) maxNumber = number;
  }
  return `SPEAKER_${(maxNumber + 1).toString().padStart(2, '0')}`;
}
