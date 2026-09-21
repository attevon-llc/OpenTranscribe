/**
 * GH #970. Modeled on `chatErrors.test.ts` (#964) / `mediaErrors.test.ts` (#960): exhaustive
 * over `ReasoningOffSwitch`, `ContextWindowStatus`, and the context-window `relation` union
 * (each typed as a `Record<..., string>` in `llmProbeVerdicts.ts`, so a new member fails to
 * compile without an entry here too), and loads the REAL `en` locale bundle, so it fails if
 * a key is missing from the JSON just as much as if a mapper is wrong.
 *
 * Must-see-red case: before this fix, `LLMSettings.svelte` rendered
 * `$t(\`settings.llmProvider.reasoningOffSwitch.${result.off_switch}\`)` (and the two
 * context-window equivalents) directly — any member with no backing key would have
 * rendered the raw dotted identifier.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import {
  resolveReasoningOffSwitchI18nKey,
  resolveContextWindowStatusI18nKey,
  resolveContextWindowRelationI18nKey,
  type ContextWindowRelation,
} from './llmProbeVerdicts';
import type { ReasoningOffSwitch, ContextWindowStatus } from '$lib/api/llmSettings';

const ALL_REASONING_OFF_SWITCHES: ReasoningOffSwitch[] = [
  'unknown',
  'unsupported',
  'no_reasoning',
  'absent',
  'works',
];

const ALL_CONTEXT_WINDOW_STATUSES: ContextWindowStatus[] = [
  'unknown',
  'unsupported',
  'not_found',
  'unreachable',
  'measured',
];

const ALL_CONTEXT_WINDOW_RELATIONS: ContextWindowRelation[] = ['below', 'above', 'match'];

describe('LLM provider probe verdict copy (#970)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it.each(ALL_REASONING_OFF_SWITCHES)(
    'renders human text for ReasoningOffSwitch "%s"',
    (offSwitch) => {
      const key = resolveReasoningOffSwitchI18nKey(offSwitch);
      const text = i18next.t(key);

      expect(text).not.toBe(key);
      expect(text).not.toBe(`settings.llmProvider.reasoningOffSwitch.${offSwitch}`);
      expect(text.length).toBeGreaterThan(0);
    }
  );

  it.each(ALL_CONTEXT_WINDOW_STATUSES)(
    'renders human text for ContextWindowStatus "%s"',
    (status) => {
      const key = resolveContextWindowStatusI18nKey(status);
      const text = i18next.t(key);

      expect(text).not.toBe(key);
      expect(text).not.toBe(`settings.llmProvider.contextWindow.${status}`);
      expect(text.length).toBeGreaterThan(0);
    }
  );

  it.each(ALL_CONTEXT_WINDOW_RELATIONS)(
    'renders human text for context-window relation "%s"',
    (relation) => {
      const key = resolveContextWindowRelationI18nKey(relation);
      const text = i18next.t(key, { window: 1000, configured: 500 });

      expect(text).not.toBe(key);
      expect(text).not.toBe(`settings.llmProvider.contextWindow.measured_${relation}`);
      expect(text.length).toBeGreaterThan(0);
    }
  );
});
