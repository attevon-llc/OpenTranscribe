import axiosInstance from '$lib/axios';
import type { AuditLogEntry } from '$lib/api/admin';

/**
 * Organization-scoped takedown (issue #1253): `/org-admin/files/...`.
 *
 * Deliberately NOT part of {@link AdminApi}. That class carries the deployment-wide
 * platform queue, and its `listQuarantinedFiles` comment forbids reusing it for an
 * organization view. These routes only ever return the caller's own organization's
 * files, are 404 unless the `org.takedown` capability is on, and need an organization
 * administrator.
 */

/** Which authority holds a taken-down file. "Platform wins": only the operator can
 * release a `platform` takedown. */
export type TakedownOrigin = 'platform' | 'organization';

export interface OrgQuarantinedFile {
  uuid: string;
  filename: string | null;
  title: string | null;
  owner_uuid: string;
  /** A name, or an address only while the owner is still a member. */
  owner_display: string;
  quarantined_at: string | null;
  legal_hold: boolean;
  is_quarantined: boolean;
  takedown_origin: TakedownOrigin;
  /** Computed by the server; never re-derive the authority rules in the UI. */
  can_release: boolean;
  /** Absent (not null) for a file the operator holds. */
  quarantine_reason?: string | null;
  /** Absent (not null) for a file the operator holds. */
  quarantined_by_display?: string | null;
}

export interface OrgQuarantinedFilesList {
  files: OrgQuarantinedFile[];
  total: number;
}

export interface OrgTakedownActionResult {
  uuid: string;
  is_quarantined: boolean;
  legal_hold: boolean;
  status: string;
  takedown_origin: TakedownOrigin | null;
}

/** The filters shared by the organization audit list and its export. Empty values are omitted. */
export interface OrgAuditQuery {
  start_date?: string;
  end_date?: string;
  event_type?: string;
  outcome?: string;
  user_uuid?: string;
  resource_type?: string;
  resource_uuid?: string;
}

/** An organization hold as its administrators see it (issue #1276). */
export interface OrgHold {
  uuid: string;
  scope: 'file' | 'collection';
  collection_uuid: string | null;
  collection_name: string | null;
  matter_ref: string | null;
  /** Visible to this organization's administrators only. */
  reason: string;
  placed_at: string;
  /** A name, or null once the person is no longer a member. */
  placed_by_display: string | null;
  released_at: string | null;
  released_by_display: string | null;
  release_reason: string | null;
  active: boolean;
  file_count: number;
}

/** A file a hold covers, as listed on the single-hold read. */
export interface OrgHeldFile {
  uuid: string;
  title: string | null;
  filename: string | null;
}

export interface OrgHoldCreate {
  scope: 'file' | 'collection';
  file_uuids?: string[];
  collection_uuid?: string;
  reason: string;
  matter_ref?: string;
}

export class OrgAdminApi {
  /** This organization's taken-down files, newest first. */
  static async listQuarantinedFiles(params?: {
    limit?: number;
    offset?: number;
    include_legal_holds?: boolean;
  }): Promise<OrgQuarantinedFilesList> {
    const response = await axiosInstance.get('/org-admin/files/quarantined', {
      params: {
        limit: params?.limit,
        offset: params?.offset,
        include_legal_holds: params?.include_legal_holds,
      },
    });
    return response.data;
  }

  /**
   * Take one of this organization's files down. Never places a legal hold. 409
   * `takedown_platform_origin` when the operator already holds the file.
   */
  static async quarantineFile(fileUuid: string, reason: string): Promise<OrgTakedownActionResult> {
    const response = await axiosInstance.post(`/org-admin/files/${fileUuid}/quarantine`, {
      reason,
    });
    return response.data;
  }

  /**
   * Release a file this organization took down. 403 `takedown_platform_origin` when the
   * operator placed the takedown; 409 when the file is not held (already released).
   */
  static async releaseFile(fileUuid: string, reason?: string): Promise<OrgTakedownActionResult> {
    const response = await axiosInstance.post(
      `/org-admin/files/${fileUuid}/release`,
      reason ? { reason } : {}
    );
    return response.data;
  }

  /**
   * This organization's audit events (`GET /org-admin/audit-logs`). Members are given by uuid:
   * a uuid outside the organization is a 403, so the filter is never a probe for who exists.
   */
  static async listAuditLogs(params: OrgAuditQuery & { limit?: number; offset?: number }): Promise<{
    logs: AuditLogEntry[];
    total: number;
  }> {
    const response = await axiosInstance.get('/org-admin/audit-logs', { params });
    return { logs: response.data.logs ?? [], total: response.data.total ?? 0 };
  }

  /**
   * Download the same view (`GET /org-admin/audit-logs/export`, up to 10,000 rows). A 404
   * means the deployment withholds the export capability.
   */
  static async exportAuditLogs(format: 'csv' | 'json', params: OrgAuditQuery): Promise<Blob> {
    const response = await axiosInstance.get('/org-admin/audit-logs/export', {
      params: { ...params, format },
      responseType: 'blob',
    });
    return response.data;
  }

  /** This organization's holds, newest first. `active_only` defaults to true on the server. */
  static async listHolds(params?: {
    active_only?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<{ holds: OrgHold[]; total: number }> {
    const response = await axiosInstance.get('/org-admin/holds', { params });
    return { holds: response.data.holds ?? [], total: response.data.total ?? 0 };
  }

  /** One hold with the files it covers (up to 500). 404 for a hold of another organization. */
  static async getHold(holdUuid: string): Promise<OrgHold & { files: OrgHeldFile[] }> {
    const response = await axiosInstance.get(`/org-admin/holds/${holdUuid}`);
    return response.data;
  }

  /**
   * Hold named files, or a whole collection (its files now and every file added later). 404
   * for a file or collection that is not this organization's; 422 for a reason outside 10 to
   * 2,000 characters.
   */
  static async placeHold(body: OrgHoldCreate): Promise<OrgHold> {
    const response = await axiosInstance.post('/org-admin/holds', body);
    return response.data;
  }

  /** Release a hold. 409 when it was already released. */
  static async releaseHold(holdUuid: string, reason: string): Promise<OrgHold> {
    const response = await axiosInstance.post(`/org-admin/holds/${holdUuid}/release`, { reason });
    return response.data;
  }
}
