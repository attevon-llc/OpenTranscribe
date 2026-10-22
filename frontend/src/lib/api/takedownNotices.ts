import axiosInstance from '$lib/axios';

/**
 * Takedown notice records (issue #1254).
 *
 * `/admin/takedown-notices/...` is the platform administrator's record of legal notices and
 * what became of each file they cover. `/takedown-notices/mine` is the signed-in owner's own
 * history: it carries no internal basis, requester reference or handler.
 */

export type NoticeType = 'dmca' | 'court_order' | 'abuse_report' | 'aup' | 'other';
export type StoredNoticeType = NoticeType | 'operator_action';
export type LinkStatus = 'open' | 'counter_notice' | 'upheld' | 'released';

export interface TakedownLink {
  uuid: string;
  file_uuid: string;
  /** The live title while the file exists; null once it was purged. */
  file_title: string | null;
  file_exists: boolean;
  owner_user_uuid: string;
  organization_uuid: string | null;
  owner_reason: string;
  status: LinkStatus;
  quarantined_at: string;
  counter_notice_received_at: string | null;
  outcome_at: string | null;
  outcome_note: string | null;
  legal_hold_placed: boolean;
}

export interface TakedownNotice {
  uuid: string;
  notice_type: StoredNoticeType;
  received_at: string;
  requester_reference: string | null;
  basis: string;
  recorded_by_authority: 'platform' | 'organization';
  handled_by_user_uuid: string | null;
  created_at: string;
  /** Derived by the server from the links: open while any file is open or contested. */
  status: 'open' | 'resolved';
  links: TakedownLink[];
}

export interface TakedownNoticeList {
  notices: TakedownNotice[];
  total: number;
}

export interface NewTakedownNotice {
  notice_type: NoticeType;
  /** ISO 8601 with an offset; not in the future. */
  received_at: string;
  requester_reference: string;
  basis: string;
}

export interface NotifyResult {
  sent: number;
  failed: number;
  skipped_no_transport: number;
  already_notified: number;
}

export interface OwnerTakedown {
  link_uuid: string;
  file_uuid: string;
  file_title: string | null;
  notice_type: StoredNoticeType;
  received_at: string;
  owner_reason: string;
  status: LinkStatus;
  quarantined_at: string;
  outcome_at: string | null;
  /** Who a counter-notice goes to. */
  dispute_to: 'operator' | 'organization';
  abuse_contact_email: string | null;
}

export interface OwnerTakedownList {
  takedowns: OwnerTakedown[];
  total: number;
}

export type ExportFormat = 'csv' | 'json';

export class TakedownNoticesApi {
  static async create(notice: NewTakedownNotice): Promise<TakedownNotice> {
    const response = await axiosInstance.post('/admin/takedown-notices', notice);
    return response.data;
  }

  static async list(params?: {
    status?: 'open' | 'resolved';
    limit?: number;
    offset?: number;
  }): Promise<TakedownNoticeList> {
    const response = await axiosInstance.get('/admin/takedown-notices', {
      params: { status: params?.status, limit: params?.limit, offset: params?.offset },
    });
    return response.data;
  }

  /** One notice with its files, as they are now. */
  static async get(noticeUuid: string): Promise<TakedownNotice> {
    const response = await axiosInstance.get(`/admin/takedown-notices/${noticeUuid}`);
    return response.data;
  }

  /** Record a counter-notice or an upheld decision. The file's state does not change. */
  static async recordOutcome(
    noticeUuid: string,
    fileUuid: string,
    status: 'counter_notice' | 'upheld'
  ): Promise<TakedownNotice> {
    const response = await axiosInstance.patch(
      `/admin/takedown-notices/${noticeUuid}/files/${fileUuid}`,
      { status }
    );
    return response.data;
  }

  /** Email each affected owner once. Recipients are chosen by the server, never sent. */
  static async notify(
    noticeUuid: string,
    event: 'quarantined' | 'released' = 'quarantined'
  ): Promise<NotifyResult> {
    const response = await axiosInstance.post(`/admin/takedown-notices/${noticeUuid}/notify`, {
      event,
    });
    return response.data;
  }

  /** The compliance export; `truncated` is true when the server stopped at its row cap. */
  static async export(
    format: ExportFormat
  ): Promise<{ blob: Blob; filename: string; truncated: boolean }> {
    const response = await axiosInstance.get('/admin/takedown-notices/export', {
      params: { format },
      responseType: 'blob',
    });
    const disposition = String(response.headers['content-disposition'] ?? '');
    const match = /filename="([^"]+)"/.exec(disposition);
    return {
      blob: response.data,
      filename: match?.[1] ?? `takedown-notices.${format}`,
      truncated: response.headers['x-export-truncated'] === 'true',
    };
  }

  /** The signed-in user's own takedowns. */
  static async listMine(params?: { limit?: number; offset?: number }): Promise<OwnerTakedownList> {
    const response = await axiosInstance.get('/takedown-notices/mine', {
      params: { limit: params?.limit, offset: params?.offset },
    });
    return response.data;
  }
}
