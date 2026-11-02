/**
 * The three on-demand checks the LLM provider panel runs against a saved configuration: a
 * connection test, the "does this model honour a do-not-reason request" probe (#64), and the
 * context-window discovery (#533). Each calls the API and reports the verdict as a toast; none
 * throws. The panel only tracks which one is in flight.
 */
import {
  LLMSettingsApi,
  type ContextWindowCapability,
  type ReasoningCapability,
  type UserLLMSettings,
} from '$lib/api/llmSettings';
import type { Translate } from '$lib/i18n';
import {
  CONTEXT_WINDOW_RELATION_KEYS,
  CONTEXT_WINDOW_STATUS_KEYS,
  REASONING_OFF_SWITCH_KEYS,
  keyFor,
} from '$lib/i18n/keyMaps';
import { getErrorMessage } from '$lib/utils/apiError';
import { toastStore } from '$stores/toast';

export type ProbeToast = {
  level: 'success' | 'info' | 'warning';
  text: string;
  durationMs: number;
};

/** The verdict toast of a reasoning probe: only a model that obeys the off switch is a success. */
export function reasoningProbeToast(
  config: UserLLMSettings,
  result: ReasoningCapability,
  t: Translate
): ProbeToast {
  const message = t(
    keyFor(REASONING_OFF_SWITCH_KEYS, result.off_switch, REASONING_OFF_SWITCH_KEYS.unknown)
  );
  return {
    level: result.off_switch === 'works' ? 'success' : 'info',
    text: `${config.name}: ${message}`,
    durationMs: 8000,
  };
}

/**
 * The verdict toast of a context-window probe. Both mismatch directions are actionable, not
 * celebratory: 'below' wastes the model, 'above' breaks requests.
 */
export function contextWindowToast(
  config: UserLLMSettings,
  result: ContextWindowCapability,
  t: Translate
): ProbeToast {
  if (result.status === 'measured' && result.relation) {
    const message = t(CONTEXT_WINDOW_RELATION_KEYS[result.relation], {
      window: result.context_window ?? 0,
      configured: result.configured_max_tokens ?? 0,
    });
    return result.relation === 'match'
      ? { level: 'success', text: `${config.name}: ${message}`, durationMs: 8000 }
      : { level: 'warning', text: `${config.name}: ${message}`, durationMs: 10000 };
  }
  const message = t(
    keyFor(CONTEXT_WINDOW_STATUS_KEYS, result.status, CONTEXT_WINDOW_STATUS_KEYS.unknown)
  );
  return { level: 'info', text: `${config.name}: ${message}`, durationMs: 8000 };
}

function show(toast: ProbeToast) {
  toastStore[toast.level](toast.text, toast.durationMs);
}

function showFailure(config: UserLLMSettings, err: unknown, t: Translate) {
  const errorMsg = getErrorMessage(err, t('settings.llmProvider.testFailed'));
  toastStore.error(`${config.name}: ${errorMsg}`, 8000);
}

export async function runConnectionTest(config: UserLLMSettings, t: Translate): Promise<void> {
  try {
    const result = await LLMSettingsApi.testConfiguration(config.uuid);
    if (result.success) {
      toastStore.success(`${config.name}: ${result.message}`, 5000);
    } else {
      toastStore.error(`${config.name}: ${result.message}`, 8000);
    }
  } catch (err: unknown) {
    showFailure(config, err, t);
  }
}

/**
 * Measure whether this model honours a "do not reason" request (issue #64).
 *
 * Explicitly user-invoked, never automatic: it costs THREE real generations against the
 * configured endpoint. Putting it in front of a chat turn would make the first question minutes
 * slow, and running it on a schedule would dial every configured third-party provider, and bill
 * for it, unprompted. The verdict decides whether the chat panel offers a reasoning toggle at
 * all, because a provider accepting the parameter is not evidence the model obeys it.
 */
export async function runReasoningProbe(config: UserLLMSettings, t: Translate): Promise<void> {
  toastStore.info(t('settings.llmProvider.probeReasoningRunning', { name: config.name }), 6000);
  try {
    show(
      reasoningProbeToast(config, await LLMSettingsApi.probeReasoningCapability(config.uuid), t)
    );
  } catch (err: unknown) {
    showFailure(config, err, t);
  }
}

/**
 * Discover the model's maximum context window from its own server (#533). `max_tokens` IS the
 * context window the app drives the model at, and its 8192 default silently caps a 60k/256k
 * model; nothing else in the UI says the window is the binding constraint on how much transcript
 * the model sees. One metadata call; the server records the measurement.
 */
export async function runContextWindowProbe(config: UserLLMSettings, t: Translate): Promise<void> {
  try {
    show(contextWindowToast(config, await LLMSettingsApi.probeContextWindow(config.uuid), t));
  } catch (err: unknown) {
    showFailure(config, err, t);
  }
}
