/**
 * GH #970, reusing #964/#960's exhaustive-Record pattern (see `chatErrors.ts` /
 * `mediaErrors.ts`) rather than inventing a third shape.
 *
 * `DerivedSource` (`$lib/types/media.ts`) is the backend-owned wire vocabulary for where a
 * derived field's value (recorded date, participants, topics, ...) came from.
 * `ProvenanceField.svelte` used to build its i18n key by interpolating `provenance.source`
 * straight into a template literal — exactly the construct that made #964 render nine raw
 * keys. `Record<DerivedSource, string>` has no index signature, so TypeScript refuses to
 * compile if `DerivedSource` gains a member with no mapping here.
 */
import type { DerivedSource } from '$lib/types/media';

const DERIVED_SOURCE_I18N_KEY: Record<DerivedSource, string> = {
  container: 'provenance.source.container',
  filename: 'provenance.source.filename',
  transcript: 'provenance.source.transcript',
  llm: 'provenance.source.llm',
  manual: 'provenance.source.manual',
  none: 'provenance.source.none',
};

export function resolveProvenanceSourceI18nKey(source: DerivedSource): string {
  return DERIVED_SOURCE_I18N_KEY[source];
}
