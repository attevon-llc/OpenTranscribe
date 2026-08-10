/**
 * API client for the account-approval queue (`/api/admin/user-approvals`, admin).
 *
 * Mirrors `backend/app/schemas/approval.py`. The queue only fills while
 * `require_account_approval` is on (a super_admin setting on the Authentication →
 * Local tab); an admin works the queue the setting produces.
 */
import axiosInstance from '../axios';

/**
 * One account awaiting a decision.
 *
 * `auth_type` is load-bearing: "someone self-registered" and "an identity provider
 * minted this on first login" are very different things to be approving, and they
 * are indistinguishable from the address alone.
 */
export interface PendingAccount {
  uuid: string;
  email: string;
  full_name: string | null;
  auth_type: string;
  role: string;
  /** Whether the deployment has proved control of the address. */
  email_verified: boolean;
  created_at: string;
}

/** The account's state after a decision. */
export interface ApprovalDecisionResponse {
  uuid: string;
  email: string;
  approval_status: string;
  approved_at: string | null;
  approved_by: string | null;
}

/**
 * True when an error is the "already decided" 409 from
 * `POST /user-approvals/{uuid}/approve|reject`.
 *
 * The backend refuses to re-decide rather than treating it as an idempotent no-op:
 * a second approve would rewrite `approved_by`/`approved_at`, and a second reject
 * would revoke the sessions of a working account. It means somebody else worked
 * the queue first, so the caller must say so and reload — never swallow it.
 */
export function isAlreadyDecided(error: unknown): boolean {
  return (error as { response?: { status?: number } } | undefined)?.response?.status === 409;
}

export class UserApprovalsApi {
  /** The pending queue, oldest first — the person waiting longest is next. */
  static async list(limit: number = 200, offset: number = 0): Promise<PendingAccount[]> {
    const response = await axiosInstance.get('/admin/user-approvals', {
      params: { limit, offset },
    });
    return response.data;
  }

  static async approve(userUuid: string, reason?: string): Promise<ApprovalDecisionResponse> {
    const response = await axiosInstance.post(
      `/admin/user-approvals/${userUuid}/approve`,
      reason ? { reason } : null
    );
    return response.data;
  }

  /**
   * Refuse an account. The row survives — the audit trail has to outlast the
   * decision, and releasing the address would let the same person sign up again
   * looking new.
   */
  static async reject(userUuid: string, reason?: string): Promise<ApprovalDecisionResponse> {
    const response = await axiosInstance.post(
      `/admin/user-approvals/${userUuid}/reject`,
      reason ? { reason } : null
    );
    return response.data;
  }
}
