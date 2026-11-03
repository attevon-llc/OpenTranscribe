/**
 * View rules of the privacy controls (issue #1280). The server decides every outcome (which
 * layer is off, whether a purge is allowed); this only picks copy and decides what is worth
 * rendering. Every i18n key here is a literal, never built from a variable (`keyMaps.test.ts`
 * explains why).
 */
import type { PrivacyLayer } from '$lib/api/privacy';

export interface OrgPrivacyAccess {
  /** Build-time constant: false in the community edition, where no organizations exist. */
  isCloudEdition: boolean;
  /** `org.defaults` is granted for this request. */
  capabilityEnabled: boolean;
  /** The audience the backend attached to the capability. */
  audience: string | undefined;
  /** `org:admin` on the signed-in user. */
  orgRole: string | undefined;
}

/**
 * Whether the organization privacy panel is offered. All four must hold: a managed edition, the
 * capability granted, aimed at organization administrators, and the person is one. The server
 * enforces the same on every route (404 with the key off, even for a superuser), so this only
 * keeps a row from advertising a destination that would refuse.
 */
export function orgPrivacyVisible(access: OrgPrivacyAccess): boolean {
  return (
    access.isCloudEdition &&
    access.capabilityEnabled &&
    access.audience === 'org_admin' &&
    access.orgRole === 'org:admin'
  );
}

/** Who turned a feature off, as a sentence for the person who cannot turn it on. */
export const LOCKED_BY_KEYS: Record<PrivacyLayer, string> = {
  platform: 'privacy.lockedBy.platform',
  instance: 'privacy.lockedBy.instance',
  organization: 'privacy.lockedBy.organization',
  user: 'privacy.lockedBy.user',
  consent: 'privacy.lockedBy.consent',
  file: 'privacy.lockedBy.file',
};

/** Layers a person at `organization` level cannot override: the ones above it. */
export function isLockedAboveOrganization(layer: PrivacyLayer | null | undefined): boolean {
  return layer === 'platform' || layer === 'instance';
}

/** Layers a plain user cannot override: every one above the user's own switch. */
export function isLockedAboveUser(layer: PrivacyLayer | null | undefined): boolean {
  return layer === 'platform' || layer === 'instance' || layer === 'organization';
}

/** `detail.code` of an error body shaped `{ detail: { code } }`, or undefined. */
export function privacyErrorCode(error: unknown): string | undefined {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  const code = (detail as { code?: unknown } | null | undefined)?.code;
  return typeof code === 'string' ? code : undefined;
}

const ERROR_KEYS: Record<string, string> = {
  policy_locked: 'privacy.error.policyLocked',
  policy_still_enabled: 'privacy.error.stillEnabled',
  purge_scope_not_allowed: 'privacy.error.scopeNotAllowed',
};

/** The translated sentence for a known error code; otherwise the caller's generic key. */
export function privacyErrorKey(error: unknown, fallbackKey: string): string {
  const code = privacyErrorCode(error);
  return (code && ERROR_KEYS[code]) || fallbackKey;
}

type GroupedCounts = Record<string, Record<string, number>> | null | undefined;

const finite = (n: number) => (Number.isFinite(n) ? n : 0);

// Counters a purge reports about what it LEFT (held or taken-down recordings, profiles that keep
// one of their voices). They ride in the same grouped counts as the deletions but are not deletions.
const PROTECTED_RECORDING_KEYS = ['files_skipped_held', 'files_skipped_quarantined'];
const PROTECTED_KEYS = [...PROTECTED_RECORDING_KEYS, 'profiles_retained'];

/** The sum of one store's deletion counts (names of kinds are not shown; kept-counters excluded). */
export function purgeStoreTotal(counts: GroupedCounts, store: string): number {
  return Object.entries(counts?.[store] ?? {})
    .filter(([kind]) => !PROTECTED_KEYS.includes(kind))
    .reduce((sum, [, n]) => sum + finite(n), 0);
}

/** How many recordings a purge left alone because of a legal hold or a takedown. */
export function purgeProtectedTotal(counts: GroupedCounts): number {
  return Object.values(counts ?? {}).reduce(
    (sum, store) =>
      sum + PROTECTED_RECORDING_KEYS.reduce((inner, key) => inner + finite(store?.[key] ?? 0), 0),
    0
  );
}

/** True when the purge kept anything at all (recordings or profiles). */
function purgeKeptAnything(counts: GroupedCounts): boolean {
  return Object.values(counts ?? {}).some((store) =>
    PROTECTED_KEYS.some((key) => finite(store?.[key] ?? 0) > 0)
  );
}

/** The sum of every count a purge reports, over all stores. */
export function purgeCountTotal(counts: GroupedCounts): number {
  return Object.keys(counts ?? {}).reduce((sum, store) => sum + purgeStoreTotal(counts, store), 0);
}

export type PurgeVerification =
  | { state: 'verified' }
  | { state: 'verified_except_protected' }
  | { state: 'remaining'; remaining: number }
  | { state: 'unverified'; unverified: number }
  | { state: 'unknown' };

/**
 * What the post-purge measurement says. Anything left over wins over "could not check", which
 * wins over "all zero": an unmeasured store is never reported as an empty one.
 */
export function purgeVerification(status: {
  residual: Record<string, number> | null;
  residual_ok: boolean | null;
  residual_unverified: string[];
  counts?: GroupedCounts;
}): PurgeVerification {
  // `== null` also covers a server that predates the measurement and omits the fields.
  if (status.residual == null || status.residual_ok == null) return { state: 'unknown' };
  const remaining = Object.values(status.residual).reduce((sum, n) => sum + finite(n), 0);
  if (remaining > 0) return { state: 'remaining', remaining };
  const unverified = status.residual_unverified ?? [];
  if (unverified.length > 0) {
    return { state: 'unverified', unverified: unverified.length };
  }
  if (!status.residual_ok) return { state: 'unknown' };
  // Measured clean, but not because everything was deleted: some recordings were kept.
  return purgeKeptAnything(status.counts)
    ? { state: 'verified_except_protected' }
    : { state: 'verified' };
}
