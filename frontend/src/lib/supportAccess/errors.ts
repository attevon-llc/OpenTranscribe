import type { SupportGrantErrorCode } from '$lib/api/supportAccess';

export type { SupportGrantErrorCode };

const KNOWN_CODES: ReadonlySet<string> = new Set<SupportGrantErrorCode>([
  'support_grant_expired',
  'support_grant_revoked',
  'support_grant_not_active',
  'support_grant_invalid',
  'support_access_unavailable',
  'support_grant_write_required',
  'support_grant_action_not_permitted',
  'support_access_audit_unavailable',
  'support_grant_already_decided',
  'support_grant_lapsed',
  'support_grant_self_approval',
  'support_grant_invalid_target',
  'support_grant_duration_exceeds_request',
]);

/** The support-access `detail.code` of an API error, or null for any other failure. */
export function supportGrantErrorCode(err: unknown): SupportGrantErrorCode | null {
  const code = (err as { response?: { data?: { detail?: { code?: unknown } } } } | undefined)
    ?.response?.data?.detail?.code;
  return typeof code === 'string' && KNOWN_CODES.has(code) ? (code as SupportGrantErrorCode) : null;
}

const SESSION_ENDING: ReadonlySet<SupportGrantErrorCode> = new Set([
  'support_grant_expired',
  'support_grant_revoked',
  'support_grant_not_active',
  'support_grant_invalid',
  'support_access_unavailable',
]);

/** True when the server is saying the grant itself is dead (not merely "this action is refused"). */
export function isSessionEndingCode(code: SupportGrantErrorCode | null): boolean {
  return code !== null && SESSION_ENDING.has(code);
}
