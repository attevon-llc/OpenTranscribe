/**
 * A copy of `defaults` where each key takes the value from `source` unless that is `undefined`
 * or `null`. Keys of `source` that `defaults` does not name are dropped, so a form model never
 * picks up fields (such as a secret) that its defaults table deliberately omits.
 */
export function withDefaults<T extends object>(defaults: T, source: Partial<T>): T {
  const merged = { ...defaults };
  for (const key of Object.keys(defaults) as (keyof T)[]) {
    const value = source[key];
    if (value !== undefined && value !== null) merged[key] = value as T[keyof T];
  }
  return merged;
}
