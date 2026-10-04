/**
 * A failed turn renders a TRANSLATED message, never provider text or a raw key (#1049).
 *
 * Observed live with Bedrock: the bubble showed "Bedrock error: An error occurred
 * (ServiceUnavailableException) when calling the ConverseStream operation (reached
 * max retries: 4)..." and the banner showed the literal `chat.errors.provider_error`.
 * i18n is initialised for real here, so a missing key would come back as the key.
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { render } from '@testing-library/svelte';

import ChatMessage from './ChatMessage.svelte';
import { initI18n } from '$lib/i18n';
import type { ChatMessage as ChatMessageType } from '$lib/types/chat';
import en from '$lib/i18n/locales/en.json';
import de from '$lib/i18n/locales/de.json';

const PROVIDER_TEXT =
  'Bedrock error: An error occurred (ServiceUnavailableException) when calling the ConverseStream operation (reached max retries: 4)';

const strings = en as Record<string, string>;

function failedTurn(overrides: Partial<ChatMessageType> = {}): ChatMessageType {
  return {
    uuid: 'assistant-1',
    role: 'assistant',
    content: '',
    status: 'error',
    error: PROVIDER_TEXT,
    ...overrides,
  };
}

function errorText(message: ChatMessageType): string {
  const { getByTestId } = render(ChatMessage, { props: { message } });
  return getByTestId('chat-message-error').textContent?.trim() ?? '';
}

describe('ChatMessage — provider error text (#1049)', () => {
  beforeAll(async () => {
    await initI18n('en');
  });

  it('renders the translated "temporarily unavailable" string for provider_unavailable', () => {
    const text = errorText(failedTurn({ msg_metadata: { error_code: 'provider_unavailable' } }));

    expect(text).toBe(strings['chat.errors.provider_unavailable']);
    expect(text).toContain('temporarily unavailable');
  });

  it('renders the translated generic provider string for provider_error', () => {
    const text = errorText(failedTurn({ msg_metadata: { error_code: 'provider_error' } }));

    expect(text).toBe(strings['chat.errors.provider_error']);
  });

  it.each(['provider_error', 'provider_unavailable', 'timeout'] as const)(
    'never shows the provider text or a raw i18n key for %s',
    (code) => {
      const text = errorText(failedTurn({ msg_metadata: { error_code: code } }));

      expect(text).not.toContain('Bedrock');
      expect(text).not.toContain('chat.errors');
      expect(text.length).toBeGreaterThan(0);
    }
  );

  it('falls back to the generic string for a legacy row that stored provider text only', () => {
    // Rows persisted before #1049 carry the raw exception in `error` and no code.
    const text = errorText(failedTurn());

    expect(text).toBe(strings['chat.message.errorGeneric']);
    expect(text).not.toContain('Bedrock');
  });

  it('is translated, not English, in another locale', async () => {
    await initI18n('de');
    const text = errorText(failedTurn({ msg_metadata: { error_code: 'provider_unavailable' } }));
    await initI18n('en');

    expect(text).toBe((de as Record<string, string>)['chat.errors.provider_unavailable']);
    expect(text).not.toBe(strings['chat.errors.provider_unavailable']);
  });
});
