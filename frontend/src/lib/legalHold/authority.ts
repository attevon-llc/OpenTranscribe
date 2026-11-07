/**
 * Who may place a legal hold from the UI, and through which API (legal-hold plan, Lane D).
 *
 * Only administrators place or release holds. In the managed edition an organization
 * administrator with `org.legal_hold` holds files of their organization (`/org-admin/holds`);
 * an instance administrator holds any file without taking it down (`/admin/holds`). Everyone
 * else gets `null` and never sees a place or release control. The server enforces the same on
 * every route; this only decides which control to render and which API the dialog calls.
 */
import type { CapabilitiesState } from '$stores/capabilities';
import { orgAdminCapOn } from '$lib/settings/orgAdminAccess';

export type HoldPlacer = 'organization' | 'platform';

/** The bounds the server enforces on a hold or release reason (`ck_content_hold_reason_length`). */
export const HOLD_REASON_MIN = 10;
export const HOLD_REASON_MAX = 2000;
/** `MAX_FILES_PER_HOLD` in `backend/app/schemas/content_hold.py`. */
export const HOLD_MAX_FILES = 500;
/** `HoldCreate.matter_ref` max_length. */
export const HOLD_MATTER_MAX = 255;

export interface HoldCaller {
  role?: string | null;
  org_role?: string | null;
}

/**
 * The hold authority the caller places holds as, or `null` when they may not place one.
 * An organization administrator acts for their organization first: that is the hold their
 * organization can see and release. Otherwise an instance admin places an operator hold.
 */
export function holdAuthorityForCaller(
  capState: CapabilitiesState,
  caller: HoldCaller | null | undefined,
  isCloudEdition: boolean
): HoldPlacer | null {
  if (!caller) return null;
  if (
    caller.org_role === 'org:admin' &&
    orgAdminCapOn(capState, 'org.legal_hold', isCloudEdition)
  ) {
    return 'organization';
  }
  if (caller.role === 'admin' || caller.role === 'super_admin') return 'platform';
  return null;
}

/** Whether a reason is one the server accepts (trimmed, 10 to 2,000 characters). */
export function holdReasonValid(reason: string): boolean {
  const length = reason.trim().length;
  return length >= HOLD_REASON_MIN && length <= HOLD_REASON_MAX;
}
