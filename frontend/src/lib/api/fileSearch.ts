/**
 * Find files by title or file name for an administrator's legal-hold and takedown pickers.
 *
 * Not the gallery list: in multi-tenant mode `GET /files` shows an administrator their own and
 * shared files only, while a hold may name any of the organization's files (organization
 * administrators) or any tenant's (instance administrators), in the trash or taken down too.
 * Each scope has its own route, gated server-side exactly like the hold routes it feeds:
 * `/org-admin/holds/files/search` (`require_org_admin`, `org.legal_hold`) and
 * `/admin/holds/files/search` (instance administrators).
 */
import axiosInstance from '$lib/axios';
import type { HoldSummary } from '$lib/types/hold';

/** Whose files: the caller's organization, or every tenant (the operator). */
export type AdminFileScope = 'organization' | 'platform';

export interface AdminFileHit {
  uuid: string;
  title: string | null;
  filename: string;
  upload_time: string | null;
  /** The owner's name, or their address when they have none. */
  owner: string;
  /** The file's organization, on the platform scope only. */
  organization_name: string | null;
  availability: 'available' | 'trashed' | 'quarantined';
  hold: HoldSummary;
}

/** Results per search: a short list to choose from, not a page to browse. */
export const FILE_SEARCH_LIMIT = 20;

export async function searchAdminFiles(
  scope: AdminFileScope,
  query: string,
  limit = FILE_SEARCH_LIMIT
): Promise<AdminFileHit[]> {
  const params = { q: query, limit };
  // Literal paths, one per scope: the route-has-a-caller scanner reads them.
  const { data } =
    scope === 'organization'
      ? await axiosInstance.get('/org-admin/holds/files/search', { params })
      : await axiosInstance.get('/admin/holds/files/search', { params });
  return Array.isArray(data?.files) ? (data.files as AdminFileHit[]) : [];
}
