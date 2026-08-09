/**
 * Admin invitations + email verification (backend v375).
 *
 * Both flows share one backend wire-contract module (`app/schemas/invitation.py`)
 * and one pair of public, rate-limited, non-enumerable endpoints, so they share
 * one client here rather than being split by URL prefix.
 *
 * Two rules the callers depend on:
 *
 * 1. **Tokens travel in the request BODY, never a header or query string.** A
 *    token in a query string lands in server access logs, browser history and
 *    referrer headers — it is a bearer credential for a not-yet-existing account.
 * 2. **Never branch on the error text.** Unknown, expired, revoked and
 *    already-used tokens all answer with one identical `detail`; classifying it
 *    client-side would rebuild the token oracle the backend deliberately removed.
 *    Render `invitationErrorMessage(err)` as-is.
 */
import axiosInstance from '../axios';

/** Core auth types. External providers register their own strings server-side. */
export type AuthType = 'local' | 'ldap' | 'oidc' | 'pki';

export const AUTH_TYPES: readonly AuthType[] = ['local', 'ldap', 'oidc', 'pki'] as const;

/** Server-computed lifecycle state — render it, never recompute from dates. */
type InvitationStatus = 'pending' | 'accepted' | 'revoked' | 'expired';

/** Non-secret facts about an invitation, for the holder of its token. */
export interface InvitationLookup {
  email: string;
  full_name: string | null;
  auth_type: string;
  /** False for ldap/oidc/pki: the IdP owns the credential. */
  requires_password: boolean;
  expires_at: string;
}

export interface InvitationAcceptResult {
  email: string;
  auth_type: string;
  can_login_with_password: boolean;
  message: string;
}

/** An invitation as an admin sees it. The raw token is never included. */
export interface Invitation {
  uuid: string;
  email: string;
  full_name: string | null;
  role: string;
  auth_type: string;
  expires_at: string;
  created_at: string;
  used_at: string | null;
  revoked_at: string | null;
  status: InvitationStatus;
}

export interface InvitationCreatePayload {
  email: string;
  full_name?: string;
  role: string;
  auth_type: AuthType;
  /** Clamped server-side to 1–336; the backend default is 72. */
  expires_in_hours?: number;
}

/** Backend default and accepted range for `expires_in_hours` (InvitationCreate). */
export const INVITE_EXPIRY_DEFAULT_HOURS = 72;
export const INVITE_EXPIRY_MIN_HOURS = 1;
export const INVITE_EXPIRY_MAX_HOURS = 336;

/**
 * Pull the backend's `detail` out of an axios error as a plain string.
 *
 * Used for the public invite/verify routes, whose `detail` is always a single
 * generic string. Callers render the result verbatim — see rule 2 above.
 */
export function invitationErrorMessage(error: unknown, fallback: string): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail;
  if (typeof detail === 'string' && detail.trim() !== '') return detail;
  if (Array.isArray(detail)) {
    const joined = detail
      .map((d: { msg?: string }) => d?.msg ?? String(d))
      .filter(Boolean)
      .join('. ');
    if (joined) return joined;
  }
  return fallback;
}

// ── public (unauthenticated) ────────────────────────────────────────────────

/** What the accept page needs to render itself. Throws on any bad-token state. */
export async function lookupInvitation(token: string): Promise<InvitationLookup> {
  const { data } = await axiosInstance.post('/auth/invitations/lookup', { token });
  return data;
}

/** Redeem an invitation. `password` is omitted for external auth types. */
export async function acceptInvitation(payload: {
  token: string;
  password?: string;
  full_name?: string;
}): Promise<InvitationAcceptResult> {
  const { data } = await axiosInstance.post('/auth/invitations/accept', payload);
  return data;
}

/** Redeem an email-verification token. */
export async function verifyEmail(token: string): Promise<{ message: string }> {
  const { data } = await axiosInstance.post('/auth/verify-email', { token });
  return data;
}

/**
 * Request a fresh verification link.
 *
 * Always resolves with the same constant message for a registered address, an
 * unknown one, and an already-verified one. Callers MUST render that message
 * unchanged: any success/failure distinction turns this into an account
 * existence oracle that needs no session.
 */
export async function resendEmailVerification(email: string): Promise<{ message: string }> {
  const { data } = await axiosInstance.post('/auth/verify-email/resend', { email });
  return data;
}

// ── admin ───────────────────────────────────────────────────────────────────

/** Invite someone to create an account. Elevated roles require super_admin. */
export async function createInvitation(payload: InvitationCreatePayload): Promise<Invitation> {
  const { data } = await axiosInstance.post('/auth/invitations', payload);
  return data;
}

/** List invitations. Pending-only unless `includeInactive`. */
export async function listInvitations(includeInactive = false): Promise<Invitation[]> {
  const { data } = await axiosInstance.get('/auth/invitations', {
    params: { include_inactive: includeInactive },
  });
  return Array.isArray(data) ? data : [];
}

/** Revoke a pending invitation. Idempotent server-side. */
export async function revokeInvitation(invitationUuid: string): Promise<void> {
  await axiosInstance.delete(`/auth/invitations/${invitationUuid}`);
}
