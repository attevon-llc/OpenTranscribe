import type { SupportGrant, UserRef } from '$lib/api/supportAccess';

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** `15 min` / `2 h`. Units, not plurals, because `ar` has six plural forms. */
export function durationLabel(minutes: number, t: Translate): string {
  if (minutes >= 60 && minutes % 60 === 0) {
    return t('supportAccess.duration.hours', { count: minutes / 60 });
  }
  return t('supportAccess.duration.minutes', { count: minutes });
}

/** A person as shown in a grant: name, else email, and an explicit label once deleted. */
export function userLabel(user: UserRef | null, t: Translate): string {
  if (!user) return t('supportAccess.deletedUser');
  return user.full_name || user.email;
}

/** The bare target name: the organization name or the subject's name (no sentence around it). */
export function targetNameOf(grant: SupportGrant, t: Translate): string {
  if (grant.target_kind === 'organization') {
    return grant.organization?.name ?? t('supportAccess.deletedOrganization');
  }
  return userLabel(grant.subject_user, t);
}

/** The target as a table cell: the organization, or "Personal workspace of X". */
export function grantTargetLabel(grant: SupportGrant, t: Translate): string {
  if (grant.target_kind === 'organization') return targetNameOf(grant, t);
  if (!grant.subject_user) return t('supportAccess.deletedUser');
  return t('supportAccess.personalWorkspace', { name: targetNameOf(grant, t) });
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** UX-only shape check; the server decides whether the file is reachable. */
export function isUuid(value: string): boolean {
  return UUID_RE.test(value.trim());
}
