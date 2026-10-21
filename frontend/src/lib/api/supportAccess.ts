/**
 * API client for support-access grants (issue #1122).
 *
 * Mirrors `backend/app/schemas/support_access.py` and backend plan section 6. Three
 * perspectives share the grant shape: platform staff (`/support-access`), a tenant's org
 * admins (`/org-admin/support-access`) and a personal-workspace owner
 * (`/users/me/support-access`). All of them 404 outside multi-tenant mode.
 */
import axiosInstance from '../axios';

export type AccessLevel = 'read' | 'write';
export type GrantMode = 'approved' | 'break_glass';
export type GrantStatus = 'pending' | 'active' | 'denied' | 'expired' | 'revoked' | 'lapsed';
export type TargetKind = 'organization' | 'personal';

export interface UserRef {
  uuid: string;
  full_name: string | null;
  email: string;
}

export interface OrgRef {
  uuid: string;
  name: string;
  slug: string | null;
}

export interface SupportGrant {
  uuid: string;
  /** Server-computed; never derive it client-side. */
  status: GrantStatus;
  target_kind: TargetKind;
  grant_mode: GrantMode;
  access_level: AccessLevel;
  /** Exactly one of organization / subject_user is set (null = the target was deleted). */
  organization: OrgRef | null;
  subject_user: UserRef | null;
  /** null = the grantee's account was deleted. */
  grantee: UserRef | null;
  reason: string;
  ticket_ref: string | null;
  requested_duration_minutes: number;
  requested_at: string;
  pending_expires_at: string | null;
  decided_by: UserRef | null;
  decided_at: string | null;
  starts_at: string | null;
  expires_at: string | null;
  revoked_by: UserRef | null;
  revoked_at: string | null;
}

export interface GrantPage {
  items: SupportGrant[];
  total: number;
  server_time: string;
}

export interface SupportGrantUse {
  occurred_at: string;
  method: string;
  /** The route TEMPLATE, e.g. `/api/files/{file_uuid}`. */
  route: string;
  resource_type: string | null;
  resource_uuid: string | null;
  need: AccessLevel | null;
}

export interface UsePage {
  items: SupportGrantUse[];
  total: number;
  server_time: string;
}

export interface OrgTarget {
  uuid: string;
  name: string;
  slug: string | null;
}

export interface CreateGrantBody {
  /** Exactly one of the two targets is non-null. */
  organization_uuid: string | null;
  subject_user_uuid: string | null;
  access_level: AccessLevel;
  /** 10..2000 characters. */
  reason: string;
  /** 15..480 minutes. */
  duration_minutes: number;
}

export interface BreakGlassBody extends CreateGrantBody {
  /** 1..255 characters; duration is 15..240. */
  ticket_ref: string;
}

export interface ApproveBody {
  /** Shorten-only: at most the requested duration. */
  duration_minutes?: number;
}

export interface DecisionNoteBody {
  note?: string;
}

/** Codes under `response.data.detail.code` (backend plan section 6.4 plus the implemented 422). */
export type SupportGrantErrorCode =
  | 'support_grant_expired'
  | 'support_grant_revoked'
  | 'support_grant_not_active'
  | 'support_grant_invalid'
  | 'support_access_unavailable'
  | 'support_grant_write_required'
  | 'support_grant_action_not_permitted'
  | 'support_access_audit_unavailable'
  | 'support_grant_already_decided'
  | 'support_grant_lapsed'
  | 'support_grant_self_approval'
  | 'support_grant_invalid_target'
  | 'support_grant_duration_exceeds_request';

export type GrantPerspective = 'staff' | 'org' | 'workspace';
/** Perspectives that can list and decide requests (staff list their own via `listMyGrants`). */
export type TenantPerspective = 'org' | 'workspace';

export interface ListParams {
  status?: GrantStatus;
  limit: number;
  offset: number;
}

const enc = encodeURIComponent;

const TENANT_BASE: Record<TenantPerspective, string> = {
  org: '/org-admin/support-access',
  workspace: '/users/me/support-access',
};

function usesPath(perspective: GrantPerspective, uuid: string): string {
  if (perspective === 'staff') return `/support-access/grants/${enc(uuid)}/uses`;
  return `${TENANT_BASE[perspective]}/${enc(uuid)}/uses`;
}

export class SupportAccessApi {
  static async listMyGrants(params: ListParams & { scope: 'mine' | 'all' }): Promise<GrantPage> {
    const response = await axiosInstance.get('/support-access/grants', { params });
    return response.data;
  }

  static async getGrant(uuid: string): Promise<SupportGrant> {
    const response = await axiosInstance.get(`/support-access/grants/${enc(uuid)}`);
    return response.data;
  }

  static async requestGrant(body: CreateGrantBody): Promise<SupportGrant> {
    const response = await axiosInstance.post('/support-access/grants', body);
    return response.data;
  }

  static async breakGlass(body: BreakGlassBody): Promise<SupportGrant> {
    const response = await axiosInstance.post('/support-access/grants/break-glass', body);
    return response.data;
  }

  /** Shared by the grantee, a super_admin, the tenant's org admin and the subject. */
  static async revokeGrant(uuid: string, body: DecisionNoteBody): Promise<SupportGrant> {
    const response = await axiosInstance.post(`/support-access/grants/${enc(uuid)}/revoke`, body);
    return response.data;
  }

  static async listUses(
    perspective: GrantPerspective,
    uuid: string,
    params: { limit: number; offset: number }
  ): Promise<UsePage> {
    const response = await axiosInstance.get(usesPath(perspective, uuid), { params });
    return response.data;
  }

  static async searchOrganizations(q: string, limit: number = 25): Promise<OrgTarget[]> {
    const response = await axiosInstance.get('/support-access/targets/organizations', {
      params: { q, limit },
    });
    return response.data;
  }

  static async listRequests(
    perspective: TenantPerspective,
    params: ListParams
  ): Promise<GrantPage> {
    const response = await axiosInstance.get(TENANT_BASE[perspective], { params });
    return response.data;
  }

  static async approve(
    perspective: TenantPerspective,
    uuid: string,
    body: ApproveBody
  ): Promise<SupportGrant> {
    const response = await axiosInstance.post(
      `${TENANT_BASE[perspective]}/${enc(uuid)}/approve`,
      body
    );
    return response.data;
  }

  static async deny(
    perspective: TenantPerspective,
    uuid: string,
    body: DecisionNoteBody
  ): Promise<SupportGrant> {
    const response = await axiosInstance.post(
      `${TENANT_BASE[perspective]}/${enc(uuid)}/deny`,
      body
    );
    return response.data;
  }
}
