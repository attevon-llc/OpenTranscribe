/**
 * GH #970. `NotificationsPanel.svelte` renders `notification.completedEnrichments`
 * (`stores/websocket.ts`, typed `string[]` — a free-form backend-emitted task name, not a
 * TypeScript union) by interpolating each task name straight into a template literal — the
 * same construct that made #964 render nine raw keys.
 *
 * Same triage conclusion as `contentRedactionVocab.ts`: no compiler check is available for
 * an open `string[]`, so this falls back to a generic key rather than rendering the raw
 * dotted identifier.
 */

const ENRICHMENT_TASK_KEYS = new Set([
  'search_indexing',
  'analytics',
  'speaker_attributes',
  'speaker_identification',
  'speaker_clustering',
]);

export const ENRICHMENT_TASK_FALLBACK_KEY = 'notifications.enrichment.other';

export function resolveEnrichmentTaskI18nKey(task: string): string {
  return ENRICHMENT_TASK_KEYS.has(task)
    ? `notifications.enrichment.${task}`
    : ENRICHMENT_TASK_FALLBACK_KEY;
}
