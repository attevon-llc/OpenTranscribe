/**
 * Account-lifecycle refusals: the narrow classifier for the 403s whose `detail` is an
 * object with a documented `code`. Pure; the store-side hold (`accountLifecycle`) and
 * its teardown rules live in `$stores/auth`.
 */

// ---------------------------------------------------------------------------
// Account lifecycle (FedRAMP AC-2 / AC-8 / IA-5)
//
// `get_current_active_user` refuses five account states with a 403 whose
// `detail` is an OBJECT, not the usual string:
//
//   {"detail": {"code": "password_change_required",       "message": "…"}}
//   {"detail": {"code": "account_expired",                "message": "…"}}
//   {"detail": {"code": "banner_acknowledgment_required", "message": "…",
//               "reason": "never_acknowledged" | "banner_text_changed"}}
//   {"detail": {"code": "account_pending_approval",       "message": "…"}}
//   {"detail": {"code": "account_rejected",               "message": "…"}}
//
// The `code` is the contract; the prose is not. Anything that string-matches the
// message breaks the moment it is reworded or translated, and every other 403
// ("Not enough permissions") still carries a plain string and must keep its
// ordinary error handling — hence the narrow classifier below rather than a
// blanket "403 means locked out".
//
// The gates fire in the order expiry → banner → password change, so a caller
// owing more than one clears them one 403 at a time; each remedy re-provokes the
// next hold rather than trying to predict it.
//
// The approval pair (`v381`) has no in-app remedy at all — the decision is
// somebody else's — and no route is exempt from it, so EVERY request answers 403.
// That is exactly why these render as a blocking screen and never as a toast: a
// toast would fire once per refused request, forever.
// ---------------------------------------------------------------------------

type AccountLifecycleCode =
  | 'password_change_required'
  | 'account_expired'
  | 'banner_acknowledgment_required'
  | 'account_pending_approval'
  | 'account_rejected';

/**
 * Why the banner gate fired. `banner_text_changed` means the user DID accept a
 * banner — different wording — so the UI must say the notice was updated rather
 * than present the refusal as a failure.
 */
type BannerAcknowledgmentReason = 'never_acknowledged' | 'banner_text_changed';

export interface AccountLifecycleState {
  code: AccountLifecycleCode;
  /** Server-supplied prose, rendered verbatim. May be empty. */
  message: string;
  /** Only carried by `banner_acknowledgment_required`; absent otherwise. */
  reason?: BannerAcknowledgmentReason;
}

const ACCOUNT_LIFECYCLE_CODES: readonly AccountLifecycleCode[] = [
  'password_change_required',
  'account_expired',
  'banner_acknowledgment_required',
  'account_pending_approval',
  'account_rejected',
];

const BANNER_ACKNOWLEDGMENT_REASONS: readonly BannerAcknowledgmentReason[] = [
  'never_acknowledged',
  'banner_text_changed',
];

/**
 * Classify an axios error as an account-lifecycle refusal.
 *
 * Returns null for every other error — including a 403 with a string `detail`,
 * an array `detail` (Pydantic validation), and an object `detail` carrying an
 * unrecognised code. Callers must fall back to their normal error path then.
 */
export function readAccountLifecycle(error: unknown): AccountLifecycleState | null {
  const response = (
    error as { response?: { status?: number; data?: { detail?: unknown } } } | undefined
  )?.response;
  if (response?.status !== 403) return null;

  const detail = response?.data?.detail;
  if (detail === null || typeof detail !== 'object' || Array.isArray(detail)) return null;

  const { code, message, reason } = detail as {
    code?: unknown;
    message?: unknown;
    reason?: unknown;
  };
  if (typeof code !== 'string') return null;
  if (!ACCOUNT_LIFECYCLE_CODES.includes(code as AccountLifecycleCode)) return null;

  const state: AccountLifecycleState = {
    code: code as AccountLifecycleCode,
    message: typeof message === 'string' ? message : '',
  };

  // An unrecognised `reason` is dropped rather than passed through: the UI
  // switches copy on it, and inventing a branch for an unknown value would show
  // the user nothing at all.
  if (
    typeof reason === 'string' &&
    BANNER_ACKNOWLEDGMENT_REASONS.includes(reason as BannerAcknowledgmentReason)
  ) {
    state.reason = reason as BannerAcknowledgmentReason;
  }

  return state;
}
