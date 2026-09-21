/**
 * GH #970. Companion to `enrichmentTasks.ts`. `completedEnrichments` is a free-form
 * `string[]` (no TS union), so this tests the every-currently-known-task case AND the
 * must-see-red case: a task name the backend could add without a matching frontend key
 * must resolve through the generic fallback, never render a raw dotted identifier.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import { resolveEnrichmentTaskI18nKey } from './enrichmentTasks';

const ALL_KNOWN_TASKS = [
  'search_indexing',
  'analytics',
  'speaker_attributes',
  'speaker_identification',
  'speaker_clustering',
];

describe('enrichment task chip copy (#970)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it.each(ALL_KNOWN_TASKS)('renders human text for known task "%s"', (task) => {
    const key = resolveEnrichmentTaskI18nKey(task);
    const text = i18next.t(key);
    expect(text).not.toBe(key);
    expect(text).not.toBe(`notifications.enrichment.${task}`);
    expect(text.length).toBeGreaterThan(0);
  });

  it('a task name with no matching frontend key falls back to the generic key, not a raw dot-key', () => {
    const key = resolveEnrichmentTaskI18nKey('future_enrichment_task');
    expect(key).toBe('notifications.enrichment.other');
    const text = i18next.t(key);
    expect(text).not.toBe(key);
    expect(text).not.toBe('notifications.enrichment.future_enrichment_task');
  });
});
