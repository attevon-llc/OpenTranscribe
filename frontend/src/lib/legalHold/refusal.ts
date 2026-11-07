/**
 * A `409 FILE_UNDER_LEGAL_HOLD` that arrives after the page loaded (legal-hold plan, Lane D).
 *
 * The file page renders frozen controls from `file.hold`, but a hold can be placed while the
 * page is open, so an edit can still be refused. `reportHoldRefusal` shows the translated
 * refusal (the same sentence `getErrorMessage` builds from `held_by`) and publishes who holds
 * the file, so the page can switch to the frozen view without a reload.
 */
import { writable } from 'svelte/store';
import { toastStore } from '$stores/toast';
import { getErrorMessage } from '$lib/utils/apiError';
import type { HoldAuthority } from '$lib/types/hold';

export const LEGAL_HOLD_ERROR_CODE = 'FILE_UNDER_LEGAL_HOLD';

interface HoldRefusalDetail {
  error: typeof LEGAL_HOLD_ERROR_CODE;
  held_by?: unknown;
}

function refusalDetail(error: unknown): HoldRefusalDetail | null {
  const response = (error as { response?: { status?: number; data?: { detail?: unknown } } })
    ?.response;
  const detail = response?.data?.detail as { error?: unknown } | null | undefined;
  if (response?.status !== 409 || !detail || detail.error !== LEGAL_HOLD_ERROR_CODE) return null;
  return detail as HoldRefusalDetail;
}

/** Whether `error` is the server refusing a write because the file is under a legal hold. */
export function isHoldRefusal(error: unknown): boolean {
  return refusalDetail(error) !== null;
}

/** The last refusal seen, with a sequence number so the same holder twice still notifies. */
export const holdRefusals = writable<{ seq: number; heldBy: HoldAuthority[] } | null>(null);
// Only ever grows, so a page subscribed before a session change never replays an old refusal.
let seq = 0;

/** Session change (`clearUserState`): forget the previous user's last refusal. */
export function resetHoldRefusals(): void {
  holdRefusals.set(null);
}

/**
 * If `error` is a legal-hold refusal: toast its translated sentence, publish it on
 * `holdRefusals` and return true (the caller then skips its own failure toast). Otherwise
 * return false and do nothing.
 */
export function reportHoldRefusal(error: unknown): boolean {
  const detail = refusalDetail(error);
  if (!detail) return false;
  const heldBy = (Array.isArray(detail.held_by) ? detail.held_by : []).filter(
    (who): who is HoldAuthority => who === 'organization' || who === 'platform'
  );
  toastStore.error(getErrorMessage(error));
  holdRefusals.set({ seq: ++seq, heldBy });
  return true;
}

/**
 * Call `onRefused` for every refusal reported from now on (not one seen before the call), with
 * who holds the file. Returns the unsubscribe function.
 */
export function onHoldRefused(onRefused: (heldBy: HoldAuthority[]) => void): () => void {
  const since = seq;
  return holdRefusals.subscribe((refusal) => {
    if (refusal && refusal.seq > since) onRefused(refusal.heldBy);
  });
}
