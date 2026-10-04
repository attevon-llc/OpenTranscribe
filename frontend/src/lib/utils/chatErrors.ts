import type { ChatErrorCode } from '$lib/types/chat';

/**
 * Every `ChatErrorCode`, as a runtime value. A `Record` rather than an array so the
 * compiler rejects a union member added without an entry here — and the i18n test
 * then rejects one added here without a `chat.errors.<code>` string in every locale.
 */
const CHAT_ERROR_CODE_SET: Record<ChatErrorCode, true> = {
  llm_unconfigured: true,
  quota_exceeded: true,
  rate_limited: true,
  provider_error: true,
  provider_unavailable: true,
  timeout: true,
  cancelled: true,
  connection_interrupted: true,
  export_failed: true,
  transcript_not_ready: true,
};

export const CHAT_ERROR_CODES = Object.keys(CHAT_ERROR_CODE_SET) as ChatErrorCode[];

export function isChatErrorCode(value: unknown): value is ChatErrorCode {
  return (
    typeof value === 'string' && Object.prototype.hasOwnProperty.call(CHAT_ERROR_CODE_SET, value)
  );
}

/**
 * The i18n key for a failed turn. Provider text (`message.error`, the frame's
 * `message`) is never rendered — it is English, and before #1049 it could carry a
 * raw SDK exception — so an unknown or missing code falls back to the generic string.
 */
export function chatErrorMessageKey(code: unknown): string {
  return isChatErrorCode(code) ? `chat.errors.${code}` : 'chat.message.errorGeneric';
}
