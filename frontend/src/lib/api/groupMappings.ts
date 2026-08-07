/**
 * API client for IdP group mappings (`/api/admin/group-mappings`, super_admin).
 *
 * Mirrors `backend/app/schemas/group_mapping.py` field-for-field. A mapping takes
 * one directory claim (an LDAP group DN, an OIDC role/group value) and grants an
 * in-app group, a role, or both.
 */
import { axiosInstance } from '../axios';

/** Mirrors `SOURCE_PATTERN` — the only two identity sources that assert groups. */
export type GroupMappingSource = 'ldap' | 'oidc';

/**
 * Mirrors `ROLE_PATTERN` (`^(user|admin)$`).
 *
 * `super_admin` is deliberately absent and must never be offered: the wire schema,
 * `assert_grantable_role` in the service, and the `ck_group_mapping_role_capped`
 * CHECK all refuse it. An IdP cannot mint a super_admin by design.
 */
export type GrantableRole = 'user' | 'admin';

export const GRANTABLE_ROLES: readonly GrantableRole[] = ['user', 'admin'];

/** A mapping as served. `group_name` saves the UI a second lookup. */
export interface GroupMapping {
  uuid: string;
  source: GroupMappingSource;
  claim_value: string;
  group_uuid: string | null;
  group_name: string | null;
  grants_role: GrantableRole | null;
  description: string | null;
  /** Directory-derived memberships in the target group (manual joins excluded). */
  member_count: number;
  created_at: string;
  updated_at: string;
}

export interface GroupMappingCreateRequest {
  source: GroupMappingSource;
  claim_value: string;
  group_uuid?: string | null;
  grants_role?: GrantableRole | null;
  description?: string | null;
}

/** All-optional patch; the server re-checks "grants something" after applying it. */
export interface GroupMappingUpdateRequest {
  claim_value?: string;
  group_uuid?: string | null;
  grants_role?: GrantableRole | null;
  description?: string | null;
}

/**
 * Ask what a claim list — or a real directory user — would resolve to.
 *
 * Exactly one of `claim_values` and `username` may be set; sending both (or
 * neither) is a 422. `username` is LDAP-only and 400s for OIDC: group membership
 * there is asserted inside a token issued to the user, so there is no
 * provider-neutral way to look it up for somebody else.
 */
export interface MappingTestRequest {
  source: GroupMappingSource;
  claim_values?: string[];
  username?: string;
}

export interface MappingTestGroup {
  uuid: string;
  name: string;
}

export interface MappingTestResponse {
  source: GroupMappingSource;
  claim_values: string[];
  matched_claims: string[];
  unmatched_claims: string[];
  groups: MappingTestGroup[];
  grants_role: GrantableRole | null;
  /**
   * True when the legacy `ldap_admin_groups` / `ldap_admin_users` configuration
   * already makes this subject an admin, independently of any mapping. Without it
   * the panel would report "no mapping grants admin" about an account that
   * nonetheless lands as one.
   */
  legacy_admin: boolean;
  effective_role: string;
}

export class GroupMappingsApi {
  /** Every configured mapping, optionally narrowed to one source. */
  static async list(source?: GroupMappingSource): Promise<GroupMapping[]> {
    const response = await axiosInstance.get('/admin/group-mappings', {
      params: source ? { source } : undefined,
    });
    return response.data;
  }

  static async create(payload: GroupMappingCreateRequest): Promise<GroupMapping> {
    const response = await axiosInstance.post('/admin/group-mappings', payload);
    return response.data;
  }

  static async update(uuid: string, payload: GroupMappingUpdateRequest): Promise<GroupMapping> {
    const response = await axiosInstance.put(`/admin/group-mappings/${uuid}`, payload);
    return response.data;
  }

  static async remove(uuid: string): Promise<void> {
    await axiosInstance.delete(`/admin/group-mappings/${uuid}`);
  }

  /** Resolve a subject against the stored mappings. Writes nothing. */
  static async test(payload: MappingTestRequest): Promise<MappingTestResponse> {
    const response = await axiosInstance.post('/admin/group-mappings/test', payload);
    return response.data;
  }
}
