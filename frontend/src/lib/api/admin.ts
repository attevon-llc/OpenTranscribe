/**
 * Admin API client for user and account management.
 */
import { axiosInstance } from '../axios';
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
   * Clear a failed-login lockout.
   *
   * `was_locked` is false when the account was not actually locked out, which the
   * caller should surface rather than reporting a no-op as a success. Note this
   * does NOT re-activate an account deactivated by {@link lockAccount} — see the
   * backend's `admin_unlock_account`.
   */
  static async unlockAccount(userUuid: string): Promise<{ success: boolean; was_locked: boolean }> {
    const response = await axiosInstance.post(`/admin/users/${userUuid}/unlock`);
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
