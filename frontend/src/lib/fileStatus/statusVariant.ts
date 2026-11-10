import type { BadgeVariant } from '$components/ui/Badge.svelte';

const SUCCESS = new Set(['completed', 'success', 'accepted', 'ok']);
const INFO = new Set(['processing', 'in_progress', 'running', 'downloading']);
const WARNING = new Set([
  'pending',
  'queued',
  'cancelling',
  'expired',
  'unverified',
  'orphaned',
  'quarantined',
]);
const ERROR = new Set(['error', 'failed', 'revoked', 'inactive', 'bad']);

/**
 * Badge variant for a task or file status. Accepts the raw status (`in_progress`) or the
 * backend's `status_badge_class` (`status-processing`), which carries the same word.
 * Unknown and neutral states (cancelled, unknown) read as the default pill.
 */
export function badgeVariantForStatus(status: string | null | undefined): BadgeVariant {
  const key = (status ?? '').toLowerCase().replace(/^status-/, '');
  if (SUCCESS.has(key)) return 'success';
  if (INFO.has(key)) return 'info';
  if (WARNING.has(key)) return 'warning';
  if (ERROR.has(key)) return 'error';
  return 'default';
}
