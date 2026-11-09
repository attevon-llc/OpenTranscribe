/**
 * Factories for the `$stores/locale` module mock. `vi.mock` is hoisted and must stay in each
 * test file; the factory bodies live here: `vi.mock('$stores/locale', async () =>
 * (await import('$testMocks/localeMocks')).englishLocale())`.
 */
import { readable } from 'svelte/store';
import en from '$lib/i18n/locales/en.json';

const english = en as Record<string, string>;

/**
 * Real English strings with `{{var}}` interpolation; falls back to the key (and, with
 * `plurals`, to `<key>_one` / `<key>_other` by `vars.count`). `code` is the `locale` store value;
 * `null` omits the store for components that never read it.
 */
export const englishLocale = (options: { code?: string | null; plurals?: boolean } = {}) => {
  const { code = 'en', plurals = false } = options;
  const t = (key: string, vars?: Record<string, unknown>) =>
    Object.entries(vars ?? {}).reduce(
      (text, [name, value]) => text.replace(`{{${name}}}`, String(value)),
      english[key] ??
        (plurals ? english[`${key}_${vars?.count === 1 ? 'one' : 'other'}`] : undefined) ??
        key
    );
  return code === null ? { t: readable(t) } : { t: readable(t), locale: readable(code) };
};

/** Real English strings, no interpolation. */
export const englishLookupLocale = () => ({
  t: readable((key: string) => english[key] ?? key),
});

/** Returns the key followed by the JSON of the options, so tests can assert the params passed. */
export const keyWithOptionsLocale = () => ({
  t: readable((key: string, opts?: Record<string, unknown>) =>
    opts ? `${key} ${JSON.stringify(opts)}` : key
  ),
});

/** Returns the key immediately followed by the JSON of the vars. */
export const keyWithVarsLocale = () => {
  const t = (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}${JSON.stringify(vars)}` : key;
  return { t: { subscribe: (run: (v: typeof t) => void) => (run(t), () => {}) } };
};
