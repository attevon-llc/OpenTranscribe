/**
 * The interface-language name of a file or task status. The API's `display_status` is English
 * text ("Completed"); the status code beside it is stable, so the label is chosen from the code.
 */
import type { Translate } from '$lib/i18n';

const STATUS_KEYS: Record<string, string> = {
  completed: 'common.completed',
  processing: 'common.processing',
  pending: 'common.pending',
  error: 'common.error',
  failed: 'fileStatus.failed',
  in_progress: 'fileStatus.inProgress',
  cancelling: 'common.cancelling',
  cancelled: 'common.cancelled',
  orphaned: 'common.orphaned',
};

/** Translated status, or `fallback` (the server's text) for a status with no key. */
export function translateFileStatus(
  t: Translate,
  status: string | null | undefined,
  fallback = ''
): string {
  const key = status ? STATUS_KEYS[status.toLowerCase().replace(/ /g, '_')] : undefined;
  return key ? t(key) : fallback || status || '';
}
