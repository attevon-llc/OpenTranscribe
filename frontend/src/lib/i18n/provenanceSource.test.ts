/**
 * GH #970. Modeled on `chatErrors.test.ts` (#964) / `mediaErrors.test.ts` (#960): exhaustive
 * over the `DerivedSource` union (typed as `Record<DerivedSource, string>` in
 * `provenanceSource.ts`, so a seventh source fails to compile without an entry here too)
 * and loads the REAL `en` locale bundle, so it fails if a key is missing from the JSON just
 * as much as if the mapper is wrong.
 *
 * This is the must-see-red case from #970: before this fix, `ProvenanceField.svelte`
 * rendered `$t(\`provenance.source.${provenance.source}\`)` directly — any `DerivedSource`
 * member with no backing key would have rendered the raw dotted identifier.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import { resolveProvenanceSourceI18nKey } from './provenanceSource';
import type { DerivedSource } from '$lib/types/media';

const ALL_DERIVED_SOURCES: DerivedSource[] = [
  'container',
  'filename',
  'transcript',
  'llm',
  'manual',
  'none',
];

describe('provenance badge copy for every DerivedSource (#970)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it.each(ALL_DERIVED_SOURCES)('renders human text for DerivedSource "%s"', (source) => {
    const key = resolveProvenanceSourceI18nKey(source);
    const text = i18next.t(key);

    // A raw dot-notation key coming back means there is no translation for it.
    expect(text).not.toBe(key);
    expect(text).not.toBe(`provenance.source.${source}`);
    expect(text.length).toBeGreaterThan(0);
  });
});
