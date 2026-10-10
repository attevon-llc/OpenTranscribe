/**
 * Admin API client for user and account management.
 */
import axiosInstance from '../axios';
import type { AuthType } from './invitations';

export interface UserSession {
  id: string;
  created_at: string;
  expires_at: string;
  ip_address: string;
  user_agent: string;
}

export interface AuditLogEntry {
  id: number;
  timestamp: string;
  event_type: string;
  user_id: number | null;
  username: string | null;
  outcome: string;
  source_ip: string;
  user_agent: string;
  details: Record<string, any>;
}

export interface AccountStatusReport {
  total_users: number;
  active_users: number;
  inactive_users: number;
  mfa_enabled_users: number;
  password_expired_users: number;
}

export interface UserSearchResult {
  uuid: string;
  email: string;
  full_name: string;
  role: string;
  auth_type: string;
  is_active: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface CreateUserPayload {
  email: string;
  full_name: string;
  role: string;
  auth_type: AuthType;
  /** Local accounts only. Ignored — and rejected server-side — for every other type. */
  password?: string;
  is_active?: boolean;
}

// ===== Locked-account management (issue #570) =====

export interface LockedAccount {
  identifier: string;
  is_locked: boolean;
  failed_attempts: number;
  lockout_count: number;
  locked_until: string | null;
  first_failed_attempt: string | null;
  last_failed_attempt: string | null;
  admin_unlocked_at: string | null;
  /** Null when `identifier` resolves to no account — an attacker-supplied
   * string typed at a login form. Escape as data; never link. */
  user_uuid: string | null;
  full_name: string | null;
  is_active: boolean | null;
  auth_type: string | null;
}

export interface LockedAccountsList {
  accounts: LockedAccount[];
  next_cursor: string | null;
  lockout_enabled: boolean;
  store_backend: 'redis' | 'memory';
  truncated: boolean;
}

export interface LockoutResetResult {
  success: boolean;
  identifier: string;
  previous_lockout_count: number;
  was_locked: boolean;
  unlocked: boolean;
}

// ===== Quarantine / takedown review queue (issue #576) =====

export interface QuarantinedFile {
  uuid: string;
  filename: string | null;
  title: string | null;
  owner_uuid: string;
  owner_email: string;
  organization_uuid: string | null;
  quarantine_reason: string | null;
  quarantined_at: string | null;
  quarantined_by_email: string | null;
  legal_hold: boolean;
  is_quarantined: boolean;
}

export interface QuarantinedFilesList {
  files: QuarantinedFile[];
  total: number;
}

export interface QuarantineActionResult {
  uuid: string;
  is_quarantined: boolean;
  legal_hold: boolean;
  status: string;
}

export class AdminApi {
  /**
   * Create a user directly (`POST /admin/users`).
   *
   * `password` is optional and must be **omitted entirely** for an external
   * `auth_type`: `UserCreate` rejects the combination with a 422 rather than
   * silently dropping the value, because a stored credential that policy will
   * never accept is worse than an error (`app/auth/utils.py:
   * local_password_allowed`). Prefer an invitation over this route — it emails
   * the invitee and never asks an admin to choose someone else's password.
   */
  static async createUser(payload: CreateUserPayload): Promise<UserSearchResult> {
    const { password, auth_type, is_active, ...rest } = payload;
    const body: Record<string, unknown> = {
      ...rest,
      auth_type,
      is_active: is_active ?? true,
      // is_superuser is derived from role server-side (mirror of super_admin)
    };
    if (auth_type === 'local' && password) {
      body.password = password;
    }
    const response = await axiosInstance.post('/admin/users', body);
    return response.data;
  }

  // Account Management
  /**
   * Admin-initiated password reset.
   *
   * The password travels in the request BODY, never as a query parameter: query
   * strings land in server access logs, browser history and referrer headers.
   * The backend (`AdminPasswordResetRequest` in `app/schemas/user.py`, consumed by
   * `POST /admin/users/{uuid}/reset-password`) accepts the body form only — the
   * query form this used to send was rejected with a 422.
   */
  static async resetUserPassword(
    userUuid: string,
    newPassword: string,
    forceChange: boolean = true
  ): Promise<void> {
    await axiosInstance.post(`/admin/users/${userUuid}/reset-password`, {
      new_password: newPassword,
      force_change: forceChange,
    });
  }

  /**
   * Clear a failed-login lockout — the true inverse of {@link lockAccount}.
   *
   * `was_locked` is false when the account was not actually locked out, which the
   * caller should surface rather than reporting a no-op as a success.
   *
   * `was_disabled` reports whether this call ALSO reactivated an account
   * deactivated by {@link lockAccount} (issue #570 §A.1.1) — the backend's
   * `admin_unlock_account` clears both the lockout and `is_active` in one call,
   * but this type previously omitted `was_disabled` entirely, so no caller could
   * read it even if it wanted to.
   */
  static async unlockAccount(
    userUuid: string
  ): Promise<{ success: boolean; was_locked: boolean; was_disabled: boolean }> {
    const response = await axiosInstance.post(`/admin/users/${userUuid}/unlock`);
    return response.data;
  }

  /**
   * List locked accounts (issue #570), cursor-paginated over the lockout store.
   *
   * `store_backend: 'memory'` means this is one process's view, not the
   * deployment's — surface that, don't hide it.
   */
  static async listLockedAccounts(params?: {
    cursor?: string | null;
    limit?: number;
    include_unlocked?: boolean;
  }): Promise<LockedAccountsList> {
    const response = await axiosInstance.get('/admin/locked-accounts', {
      params: {
        cursor: params?.cursor ?? undefined,
        limit: params?.limit,
        include_unlocked: params?.include_unlocked,
      },
    });
    return response.data;
  }

  /**
   * Reset an account's progressive lockout counter — distinct from
   * {@link unlockAccount}. Leaves an in-progress lock intact; use
   * {@link unlockAccount} to unlock immediately.
   */
  static async resetLockoutCounter(identifier: string): Promise<LockoutResetResult> {
    const response = await axiosInstance.post(
      `/admin/locked-accounts/${encodeURIComponent(identifier)}/reset`
    );
    return response.data;
  }

  // ===== Quarantine / takedown (issue #576) =====

  /**
   * The abuse/DMCA takedown review queue. Deployment-wide (every owner/org),
   * which is correct for this platform-admin surface — never reuse for an
   * org-scoped view (§B.6 E7).
   */
  static async listQuarantinedFiles(params?: {
    limit?: number;
    offset?: number;
    include_legal_holds?: boolean;
  }): Promise<QuarantinedFilesList> {
    const response = await axiosInstance.get('/admin/files/quarantined', {
      params: {
        limit: params?.limit,
        offset: params?.offset,
        include_legal_holds: params?.include_legal_holds,
      },
    });
    return response.data;
  }

  /** Take a file down (abuse/DMCA). Reversible via {@link releaseFile}. */
  static async quarantineFile(
    fileUuid: string,
    reason: string,
    legalHold: boolean = true
  ): Promise<QuarantineActionResult> {
    const response = await axiosInstance.post(`/admin/files/${fileUuid}/quarantine`, {
      reason,
      legal_hold: legalHold,
    });
    return response.data;
  }

  /**
   * Release a quarantined file, or lift a legal hold on a released-but-still-held
   * one (issue #689/#576 §B.1.2). 409 when the file is neither quarantined nor
   * held — surface that as "already released", never retry blindly.
   */
  static async releaseFile(
    fileUuid: string,
    alsoLiftLegalHold: boolean = true
  ): Promise<QuarantineActionResult> {
    const response = await axiosInstance.post(`/admin/files/${fileUuid}/release`, {
      clear_legal_hold: alsoLiftLegalHold,
    });
    return response.data;
  }

  /**
   * Deactivate an account and revoke every one of its sessions.
   *
   * `reason` is recorded verbatim in the audit log, so callers pass a stable
   * English string rather than a localised one.
   */
  static async lockAccount(userUuid: string, reason: string): Promise<{ success: boolean }> {
    const response = await axiosInstance.post(`/admin/users/${userUuid}/lock`, null, {
      params: { reason },
    });
    return response.data;
  }

  static async terminateUserSessions(userUuid: string): Promise<{ sessions_terminated: number }> {
    const response = await axiosInstance.delete(`/admin/users/${userUuid}/sessions`);
    return response.data;
  }

  static async getUserSessions(userUuid: string): Promise<{ sessions: UserSession[] }> {
    const response = await axiosInstance.get(`/admin/users/${userUuid}/sessions`);
    return response.data;
  }

  static async changeUserRole(userUuid: string, newRole: string): Promise<void> {
    await axiosInstance.put(`/admin/users/${userUuid}/role`, null, {
      params: { new_role: newRole },
    });
  }

  /** Clear the target's TOTP secret and backup codes, and revoke their sessions. */
  static async resetUserMFA(userUuid: string): Promise<{ success: boolean }> {
    const response = await axiosInstance.post(`/admin/users/${userUuid}/mfa/reset`);
    return response.data;
  }

  /**
   * The operator remedy for a source that cannot assert `email_verified`
   * (Authentik hardcodes it `false` for every account) — see
   * `auth/account_linking.py`. Sets the provider's own identifier on the
   * account so the *next* login by that identity matches directly, rather
   * than falling into (and being refused by) the automatic email-match path.
   *
   * Applying this to a `local`-password account also converts its `auth_type`
   * to `provider` and revokes its sessions (issue #912) — the returned
   * `auth_type` reflects that without a re-fetch. An account whose `auth_type`
   * is already non-local is left alone.
   */
  static async linkExternalIdentity(
    userUuid: string,
    provider: 'oidc' | 'ldap' | 'pki',
    identifier: string
  ): Promise<{ success: boolean; provider: string; identifier: string; auth_type: string }> {
    const response = await axiosInstance.put(`/admin/users/${userUuid}/link-identity`, {
      provider,
      identifier,
    });
    return response.data;
  }

  /**
   * The sibling remedy, for the OTHER way an external login gets stuck (issue #867).
   *
   * When an account is already linked by provider identifier and the IdP later changes
   * the person's address, `assert_provider_id_link_permitted` refuses the login — and
   * because that check runs before every provider's profile refresh, the stored address
   * can never catch up on its own. Every retry produces the identical 401. This accepts
   * the new address so the next ordinary login succeeds.
   *
   * Refused for an account carrying no external identifier: it is a remedy for a linked
   * identity, not a general email change. **Also refused for an `auth_type == local`
   * account** (issue #912), even one that happens to carry an identifier column — that
   * is a legitimate SCIM-provisioned state, not something this endpoint should rewrite.
   */
  static async updateExternalEmail(
    userUuid: string,
    email: string
  ): Promise<{ success: boolean; email: string; previous_email: string }> {
    const response = await axiosInstance.put(`/admin/users/${userUuid}/external-email`, {
      email,
    });
    return response.data;
  }

  // User Search
  static async searchUsers(params: {
    query?: string;
    role?: string;
    auth_type?: string;
    is_active?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<{ total: number; users: UserSearchResult[] }> {
    const response = await axiosInstance.get('/admin/users/search', { params });
    return response.data;
  }

  // Audit Logs
  static async getAuditLogs(params: {
    start_date?: string;
    end_date?: string;
    event_type?: string;
    user_id?: number;
    outcome?: string;
    limit?: number;
    offset?: number;
  }): Promise<{ logs: AuditLogEntry[]; total: number; offset: number; limit: number }> {
    const response = await axiosInstance.get('/admin/audit-logs', { params });
    const data = response.data;
    if (Array.isArray(data)) {
      return { logs: data, total: data.length, offset: 0, limit: data.length };
    }
    return {
      logs: data.logs ?? [],
      total: data.total ?? 0,
      offset: data.offset ?? 0,
      limit: data.limit ?? 100,
    };
  }

  static async exportAuditLogs(
    format: 'csv' | 'json',
    startDate?: string,
    endDate?: string
  ): Promise<Blob> {
    const response = await axiosInstance.get('/admin/audit-logs/export', {
      params: { export_format: format, start_date: startDate, end_date: endDate },
      responseType: 'blob',
    });
    return response.data;
  }

  // Reports
  static async getAccountStatusReport(): Promise<AccountStatusReport> {
    const response = await axiosInstance.get('/admin/reports/account-status');
    return response.data;
  }
}
