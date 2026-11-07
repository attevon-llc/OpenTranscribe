import { getErrorMessage } from '$lib/utils/apiError';

/**
 * The translated sentence for a trash route's 409 (issue #1101). The server's `detail.error`
 * codes come from `backend/app/services/trash_service.py`; its `message` is English, so a
 * known code is shown in the UI language. A legal-hold refusal (`FILE_UNDER_LEGAL_HOLD`) and
 * everything else go through `getErrorMessage`, which already translates the hold refusal.
 */
const REFUSAL_KEYS: Readonly<Record<string, string>> = {
  NOT_RESTORABLE: 'trash.notRestorable',
  FILE_UNDER_TAKEDOWN: 'trash.takedownRefused',
};

export function trashRefusalMessage(
  error: unknown,
  translate: (key: string) => string,
  fallback: string
): string {
  const detail = (error as { response?: { data?: { detail?: unknown } } } | null)?.response?.data
    ?.detail;
  const code =
    detail && typeof detail === 'object' ? (detail as { error?: unknown }).error : undefined;
  const key = typeof code === 'string' ? REFUSAL_KEYS[code] : undefined;
  return key ? translate(key) : getErrorMessage(error, fallback);
}
