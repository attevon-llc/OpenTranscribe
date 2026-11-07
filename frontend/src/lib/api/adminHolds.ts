import axiosInstance from '$lib/axios';
import type { OrgHeldFile, OrgHold, OrgHoldCreate } from '$lib/api/orgAdmin';

/**
 * The operator's legal holds (legal-hold plan §8.4): `/admin/holds`, instance administrators
 * only. A hold here freezes files of any account without taking them down; holds placed with a
 * takedown stay on the quarantine screens. Shapes are the organization routes' (`HoldOut`,
 * `HoldCreate`), so one panel and one dialog serve both through {@link HoldsApi}.
 */
export interface HoldsApi {
  listHolds(params?: {
    active_only?: boolean;
    limit?: number;
    offset?: number;
  }): Promise<{ holds: OrgHold[]; total: number }>;
  getHold(holdUuid: string): Promise<OrgHold & { files: OrgHeldFile[] }>;
  placeHold(body: OrgHoldCreate): Promise<OrgHold>;
  releaseHold(holdUuid: string, reason: string): Promise<OrgHold>;
}

export const AdminHoldsApi: HoldsApi = {
  /** Operator holds, newest first. `active_only` defaults to true on the server. */
  async listHolds(params) {
    const response = await axiosInstance.get('/admin/holds', { params });
    return { holds: response.data.holds ?? [], total: response.data.total ?? 0 };
  },

  /** One operator hold with the files it covers (up to 500). */
  async getHold(holdUuid) {
    const response = await axiosInstance.get(`/admin/holds/${holdUuid}`);
    return response.data;
  },

  /**
   * Hold named files of any account, or a whole collection (its files now and every file
   * added later). 404 when a file or the collection does not exist; nothing is quarantined.
   */
  async placeHold(body) {
    const response = await axiosInstance.post('/admin/holds', body);
    return response.data;
  },

  /** Release an operator hold. 409 when it was already released. */
  async releaseHold(holdUuid, reason) {
    const response = await axiosInstance.post(`/admin/holds/${holdUuid}/release`, { reason });
    return response.data;
  },
};
