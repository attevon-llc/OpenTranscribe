/**
 * Pure rules behind the speaker identity history view (issue #1336): which value of a change
 * to show, and how to name who made it. Free of the DOM and the i18n store; the component
 * translates the keys these return.
 */
import type { SpeakerIdentityEvent, SpeakerIdentityEventValues } from '$lib/types/speaker';

/** A value to show: literal text, or an i18n key for a value with no text of its own. */
export type HistoryValue = { text: string } | { key: string } | null;

/** The i18n key of an entry's kind. */
export function kindKey(event: SpeakerIdentityEvent): string {
  return `speakerHistory.kind.${event.kind}`;
}

/** The field of `before`/`after` an entry is about: a merge names the absorbed speaker. */
function primaryField(
  event: SpeakerIdentityEvent
):
  | keyof Pick<SpeakerIdentityEventValues, 'name' | 'profile' | 'suggested_name' | 'merged_name'>
  | null {
  if (event.kind === 'merge' && event.before?.merged_name != null) return 'merged_name';
  const touched = { ...(event.before ?? {}), ...(event.after ?? {}) };
  if ('name' in touched) return 'name';
  if ('profile' in touched) return 'profile';
  if ('suggested_name' in touched) return 'suggested_name';
  return null;
}

function valueOf(
  values: SpeakerIdentityEventValues | null | undefined,
  field: NonNullable<ReturnType<typeof primaryField>>
): HistoryValue {
  if (!values) return null;
  if (values.withheld?.includes(field === 'profile' ? 'profile_id' : field)) {
    return { key: 'speakerHistory.withheld' };
  }
  if (!(field in values)) return null;
  if (field === 'profile') {
    const profile = values.profile;
    if (!profile) return { key: 'speakerHistory.none' };
    if (profile.deleted || !profile.name) return { key: 'speakerHistory.deletedProfile' };
    return { text: profile.name };
  }
  const text = values[field];
  return text ? { text } : { key: 'speakerHistory.none' };
}

/**
 * What the entry changed, as `from` and `to`. `null` for an entry that changes no shown value
 * (a confirmation, a skip, a purge): the kind alone says what happened.
 */
export function changeOf(
  event: SpeakerIdentityEvent
): { from: HistoryValue; to: HistoryValue } | null {
  const field = primaryField(event);
  if (!field) return null;
  if (field === 'merged_name') return { from: valueOf(event.before, field), to: null };
  return { from: valueOf(event.before, field), to: valueOf(event.after, field) };
}

/** Who made the change: a named person, a person the viewer may not see, or the system. */
export function actorOf(event: SpeakerIdentityEvent): HistoryValue {
  if (event.actor.kind === 'system') return { key: 'speakerHistory.bySystem' };
  const user = event.actor.user;
  if (user?.erased) return { key: 'speakerHistory.byErased' };
  if (user?.name) return { text: user.name };
  return { key: 'speakerHistory.byPerson' };
}

/** A confidence in 0-1 as a whole percentage, or `null` when none was recorded. */
export function confidencePercent(event: SpeakerIdentityEvent): number | null {
  const value = event.confidence;
  return typeof value === 'number' && Number.isFinite(value) ? Math.round(value * 100) : null;
}
