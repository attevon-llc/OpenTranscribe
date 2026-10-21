import axiosInstance from '$lib/axios';

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
}
