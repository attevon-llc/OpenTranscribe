/**
 * GH #970. `ContentRedactionSettings.svelte` renders three backend-supplied option lists
 * (`RedactionSystemDefaults.available_styles` / `available_detectors` /
 * `available_categories`, `backend/app/schemas/redaction_settings.py`) by interpolating
 * each value straight into a template literal — the same construct that made #964 render
 * nine raw keys.
 *
 * Unlike `chatErrors.ts` / `mediaErrors.ts`, these three vocabularies are plain
 * `list[str]` backend constants (`REDACTION_STYLES` / `REDACTION_DETECTORS` /
 * `REDACTION_CATEGORIES` in `backend/app/core/constants.py`), not a shared TypeScript
 * union — there is no compiler check available for "every backend value has a frontend
 * key". Per #970's triage, the weaker-but-real requirement applies instead: resolve
 * through a helper that falls back to a known-good generic key
 * (`settings.contentRedaction.unrecognizedOption`, interpolated with the raw value so the
 * admin can still tell what it was) rather than rendering the raw dotted identifier.
 */

const REDACTION_STYLE_KEYS = new Set(['label', 'asterisks', 'first_letter', 'blur']);
const REDACTION_DETECTOR_KEYS = new Set(['profanity', 'pii', 'toxicity', 'llm']);
const REDACTION_CATEGORY_KEYS = new Set(['profanity', 'pii', 'toxicity', 'custom']);

export const REDACTION_UNRECOGNIZED_OPTION_KEY = 'settings.contentRedaction.unrecognizedOption';

export function resolveRedactionStyleI18nKey(style: string): string {
  return REDACTION_STYLE_KEYS.has(style)
    ? `settings.contentRedaction.styleOption.${style}`
    : REDACTION_UNRECOGNIZED_OPTION_KEY;
}

export function resolveRedactionDetectorI18nKey(detector: string): string {
  return REDACTION_DETECTOR_KEYS.has(detector)
    ? `settings.contentRedaction.detector.${detector}`
    : REDACTION_UNRECOGNIZED_OPTION_KEY;
}

export function resolveRedactionCategoryI18nKey(category: string): string {
  return REDACTION_CATEGORY_KEYS.has(category)
    ? `settings.contentRedaction.category.${category}`
    : REDACTION_UNRECOGNIZED_OPTION_KEY;
}
