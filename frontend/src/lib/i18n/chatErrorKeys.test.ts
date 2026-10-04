/**
 * Every chat error code the UI can be handed has a string in every locale (#1049).
 *
 * The chat banner renders `chat.errors.<state.error>` and the message bubble
 * `chat.errors.<error_code>`. A code without a key renders as the literal key — the
 * `chat.errors.provider_error` users saw. `check:i18n` only enforces parity with
 * en.json, so a key missing from en.json itself passed it.
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

import { CHAT_ERROR_CODES } from '$lib/utils/chatErrors';

const here = dirname(fileURLToPath(import.meta.url));
const localesDir = join(here, 'locales');
const chatStoreSource = readFileSync(join(here, '..', '..', 'stores', 'chat.ts'), 'utf8');

/** The string literals the chat store assigns to `state.error` besides frame codes. */
const storeErrorLiterals = [...chatStoreSource.matchAll(/error: '([A-Za-z_]+)'/g)].map((m) => m[1]);

const locales = readdirSync(localesDir)
  .filter((f) => f.endsWith('.json'))
  .sort();

describe('chat.errors.* key completeness', () => {
  it('finds the locale files and the store literals it is checking', () => {
    expect(locales).toHaveLength(12);
    expect(storeErrorLiterals).toEqual(expect.arrayContaining(['send', 'conversationNotFound']));
    expect(CHAT_ERROR_CODES).toEqual(
      expect.arrayContaining(['provider_error', 'provider_unavailable'])
    );
  });

  it.each(locales)('%s has a non-empty string for every chat error code', (file) => {
    const strings = JSON.parse(readFileSync(join(localesDir, file), 'utf8')) as Record<
      string,
      string
    >;
    const missing = [...CHAT_ERROR_CODES, ...storeErrorLiterals]
      .map((code) => `chat.errors.${code}`)
      .filter((key) => typeof strings[key] !== 'string' || strings[key].trim() === '');

    expect(missing).toEqual([]);
  });
});
