/**
 * Which "LLM is not usable" sentence a bulk action's per-file results call for.
 *
 * "unusable" (the person HAS a configuration and it is broken) wins over "not available" (none
 * configured): telling someone to add a provider they already have sends them the wrong way.
 */
export const BULK_LLM_UNUSABLE_KEY = 'gallery.bulk.llmConfigUnusable';
export const BULK_LLM_NOT_CONFIGURED_KEY = 'gallery.bulk.llmNotConfigured';

export function bulkLlmFailureKey(
  results: { success: boolean; error?: string | null }[]
): typeof BULK_LLM_UNUSABLE_KEY | typeof BULK_LLM_NOT_CONFIGURED_KEY | null {
  const failed = results.filter((r) => !r.success);
  if (failed.some((r) => r.error === 'LLM_CONFIG_UNUSABLE')) return BULK_LLM_UNUSABLE_KEY;
  if (failed.some((r) => r.error === 'LLM_NOT_AVAILABLE')) return BULK_LLM_NOT_CONFIGURED_KEY;
  return null;
}
