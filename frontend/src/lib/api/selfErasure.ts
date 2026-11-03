import axiosInstance from '$lib/axios';

/**
 * Self-service account erasure (issue #1354).
 *
 * Mirrors `backend/app/schemas/self_erasure.py`. Deliberately NOT part of `AdminApi`: this is
 * the account holder's own action (`admin.lockedAccountsQuarantine.test.ts` keeps operator
 * erasure methods off the admin client). While the feature is off the server answers 404 to
 * POST and DELETE, so `enabled` from {@link getSelfErasureStatus} decides whether to show it.
 */

/** The literal the API requires in `SelfErasureCreate.confirm`; the person types it. */
export const SELF_ERASURE_CONFIRM_WORD = 'ERASE';

/** The request receipt: ids, times and a digest. Never personal data. */
export interface SelfErasureReceipt {
  ledger_uuid: string;
  requested_at: string;
  scheduled_for: string | null;
  sla_due_at: string;
  request_digest: string;
  status: 'scheduled' | 'cancelled';
  cancelled_at: string | null;
}

/** A request the system closed without erasing anything. Codes only, no personal data. */
export interface SelfErasureOutcome {
  status: 'blocked';
  reason: 'last_super_admin';
  ledger_uuid: string;
  /** When the blocked request fell due. */
  at: string | null;
}

export interface SelfErasureStatus {
  enabled: boolean;
  grace_days: number;
  /** False for an account with neither a password nor MFA to confirm with. */
  can_reauthenticate: boolean;
  request: SelfErasureReceipt | null;
  /** Present when there is no open request and the latest one was blocked (optional: older servers omit it). */
  last_outcome?: SelfErasureOutcome | null;
}

export interface SelfErasureCreate {
  confirm: typeof SELF_ERASURE_CONFIRM_WORD;
  password?: string;
  mfa_code?: string;
}

/** `GET|PUT /admin/self-erasure`: the two instance settings (super administrator). */
export interface SelfErasureSettings {
  enabled: boolean;
  grace_days: number;
  max_grace_days: number;
}

export async function getSelfErasureStatus(): Promise<SelfErasureStatus> {
  const response = await axiosInstance.get('/users/me/erasure-request');
  return response.data;
}

export async function requestSelfErasure(body: SelfErasureCreate): Promise<SelfErasureReceipt> {
  const response = await axiosInstance.post('/users/me/erasure-request', body);
  return response.data;
}

export async function cancelSelfErasure(ledgerUuid: string): Promise<SelfErasureReceipt> {
  const response = await axiosInstance.delete(`/users/me/erasure-request/${ledgerUuid}`);
  return response.data;
}

export async function getSelfErasureSettings(): Promise<SelfErasureSettings> {
  const response = await axiosInstance.get('/admin/self-erasure');
  return response.data;
}

export async function putSelfErasureSettings(
  update: Partial<Pick<SelfErasureSettings, 'enabled' | 'grace_days'>>
): Promise<SelfErasureSettings> {
  const response = await axiosInstance.put('/admin/self-erasure', update);
  return response.data;
}

const ERROR_KEYS: Record<string, string> = {
  last_super_admin: 'settings.accountErasure.errorLastSuperAdmin',
  request_exists: 'settings.accountErasure.errorExists',
  reauth_required: 'settings.accountErasure.errorReauth',
  reauth_unavailable: 'settings.accountErasure.reauthUnavailable',
  already_executing: 'settings.accountErasure.errorTooLate',
  not_cancellable: 'settings.accountErasure.errorNotCancellable',
  not_found: 'settings.accountErasure.errorNotFound',
};

/** The i18n key for a refusal's `detail.code`, else the generic one. Never echoes server text. */
export function selfErasureErrorKey(error: unknown): string {
  const detail = (error as { response?: { data?: { detail?: { code?: unknown } } } })?.response
    ?.data?.detail;
  const code = typeof detail?.code === 'string' ? detail.code : '';
  return ERROR_KEYS[code] ?? 'settings.accountErasure.errorGeneric';
}
