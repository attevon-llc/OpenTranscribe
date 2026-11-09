/**
 * Narrowing helpers for values whose shape is not known at compile time
 * (parsed JSON, LLM-authored summary data, caught errors).
 */

/** A non-null, non-array object whose keys are readable as `unknown`. */
export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
