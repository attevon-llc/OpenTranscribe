/**
 * Pure rules behind the LLM provider panel (`LLMSettings.svelte`): which configuration is
 * active, what its last stored test says about the connection, and which load failures the
 * user needs to hear about.
 */
import type { UserLLMSettings } from '$lib/api/llmSettings';
import { getErrorMessage, getErrorStatus } from '$lib/utils/apiError';

export type ConnectionState =
  | { status: 'connected' | 'disconnected'; message: string; lastChecked: Date | null }
  | { status: 'unknown' };

/** The active configuration, looked up among the user's own first and then those shared with them. */
export function resolveActiveConfiguration(
  saved: UserLLMSettings[],
  shared: UserLLMSettings[],
  activeId: string | null
): UserLLMSettings | null {
  if (!activeId) return null;
  return saved.find((c) => c.uuid === activeId) || shared.find((c) => c.uuid === activeId) || null;
}

/**
 * What to show as the connection status from the configuration's last stored test, so the
 * panel does not re-test on every open. An untested configuration is `unknown`, not a failure.
 */
export function connectionFromStoredTest(config: UserLLMSettings | null): ConnectionState {
  const status = config?.test_status;
  if (config && (status === 'success' || status === 'failed')) {
    return {
      status: status === 'success' ? 'connected' : 'disconnected',
      message: config.test_message || '',
      lastChecked: config.last_tested ? new Date(config.last_tested) : null,
    };
  }
  return { status: 'unknown' };
}

/**
 * The server answers 404 or 403 when the user has no configuration list at all (no
 * configuration yet, or none visible to them). That is an empty panel, not an error.
 */
export function isNoConfigurationsAnswer(err: unknown): boolean {
  const status = getErrorStatus(err);
  return status === 404 || status === 403;
}

/**
 * The text to toast for a failed load, or `null` to stay quiet. An error that already says it
 * is about the LLM / a configuration is not repeated: the empty-state copy covers it.
 */
export function loadFailureMessage(err: unknown, fallback: string): string | null {
  const detail = getErrorMessage(err, '');
  const raw = err instanceof Error ? err.message : '';
  if (raw.includes('LLM') || detail.includes('configuration')) return null;
  return detail || fallback;
}
