import type { QuarantinedFile } from '$lib/api/admin';
import type { OrgQuarantinedFile, TakedownOrigin } from '$lib/api/orgAdmin';

export type QueueScope = 'platform' | 'organization';

/** One row of the takedown queue, whichever API produced it. */
export interface QueueRow {
  uuid: string;
  label: string;
  owner: string;
  reason: string | null;
  /** The organization queue never receives the reason of a takedown the operator placed. */
  reasonWithheld: boolean;
  quarantinedAt: string | null;
  by: string | null;
  origin: TakedownOrigin | null;
  legalHold: boolean;
  isQuarantined: boolean;
  /** Platform: always (the operator can release anything). Organization: server-computed. */
  canRelease: boolean;
  /** Notice records covering the file (platform queue only; issue #1254). */
  notices: { uuid: string; type: string; status: string }[];
}

function fromPlatform(f: QuarantinedFile): QueueRow {
  return {
    uuid: f.uuid,
    label: f.title || f.filename || f.uuid,
    owner: f.owner_email,
    reason: f.quarantine_reason,
    reasonWithheld: false,
    quarantinedAt: f.quarantined_at,
    by: f.quarantined_by_email,
    origin: f.takedown_origin ?? null,
    legalHold: f.legal_hold,
    isQuarantined: f.is_quarantined,
    canRelease: true,
    notices: (f.notices ?? []).map((n) => ({
      uuid: n.link_uuid,
      type: n.notice_type,
      status: n.status,
    })),
  };
}

function fromOrganization(f: OrgQuarantinedFile): QueueRow {
  return {
    uuid: f.uuid,
    label: f.title || f.filename || f.uuid,
    owner: f.owner_display,
    reason: f.quarantine_reason ?? null,
    reasonWithheld: f.takedown_origin === 'platform',
    quarantinedAt: f.quarantined_at,
    by: f.quarantined_by_display ?? null,
    origin: f.takedown_origin,
    legalHold: f.legal_hold,
    isQuarantined: f.is_quarantined,
    canRelease: f.can_release,
    notices: [],
  };
}

/** Normalize an API page into queue rows. */
export function toRows(
  scope: QueueScope,
  files: QuarantinedFile[] | OrgQuarantinedFile[]
): QueueRow[] {
  return scope === 'organization'
    ? (files as OrgQuarantinedFile[]).map(fromOrganization)
    : (files as QuarantinedFile[]).map(fromPlatform);
}
