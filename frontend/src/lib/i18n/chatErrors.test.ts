/**
 * GH #964. `state.error` in the chat error banner is snake_case
 * (`ChatErrorCode`) but the `chat.errors.*` i18n namespace is camelCase —
 * before the fix, `$t(\`chat.errors.${code}\`)` resolved to nothing for any
 * of the nine `ChatErrorCode` members and rendered the raw key.
 *
 * This is exhaustive over the `ChatErrorCode` union (typed as
 * `Record<ChatErrorCode, string>` in chatErrors.ts, so a tenth code fails to
 * compile without an entry here too) and loads the REAL `en` locale bundle,
 * so it fails if a key is missing from the JSON just as much as if the
 * mapper is wrong.
 */
import { describe, it, expect, beforeAll } from 'vitest';
import i18next from 'i18next';
import { initI18n } from './index';
import { resolveChatErrorI18nKey } from './chatErrors';
import type { ChatErrorCode } from '$lib/types/chat';

const ALL_CHAT_ERROR_CODES: ChatErrorCode[] = [
  'llm_unconfigured',
  'quota_exceeded',
  'rate_limited',
  'provider_error',
  'timeout',
  'cancelled',
  'connection_interrupted',
  'export_failed',
  'transcript_not_ready',
];

describe('chat error banner copy for every ChatErrorCode (#964)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it.each(ALL_CHAT_ERROR_CODES)('renders human text for ChatErrorCode "%s"', (code) => {
    const key = resolveChatErrorI18nKey(code);
    const text = i18next.t(key);

    // A raw dot-notation key coming back means there is no translation for it.
    expect(text).not.toBe(key);
    // Guards against the exact #964 bug: the snake_case code interpolated
    // straight into the namespace, which never resolves.
    expect(text).not.toBe(`chat.errors.${code}`);
    expect(text.length).toBeGreaterThan(0);
  });

  it('passes non-ChatErrorCode legacy values through unchanged (conversationNotFound, send)', () => {
    expect(resolveChatErrorI18nKey('conversationNotFound')).toBe(
      'chat.errors.conversationNotFound'
    );
    expect(resolveChatErrorI18nKey('send')).toBe('chat.errors.send');
  });
});
