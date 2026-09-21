/**
 * GH #964. `ChatErrorCode` (the wire vocabulary, snake_case) and the
 * `chat.errors.*` i18n namespace (camelCase) are independently free to
 * change — this map is the one place that has to know both. `Record<
 * ChatErrorCode, string>` has no index signature, so TypeScript refuses to
 * compile if a `ChatErrorCode` member is added here without a translated
 * key: that's what keeps this exhaustive, not a runtime check.
 */
import type { ChatErrorCode } from '$lib/types/chat';

const CHAT_ERROR_CODE_I18N_KEY: Record<ChatErrorCode, string> = {
  llm_unconfigured: 'chat.errors.llmUnconfigured',
  quota_exceeded: 'chat.errors.quotaExceeded',
  rate_limited: 'chat.errors.rateLimited',
  provider_error: 'chat.errors.providerError',
  timeout: 'chat.errors.timeout',
  cancelled: 'chat.errors.cancelled',
  connection_interrupted: 'chat.errors.connectionInterrupted',
  export_failed: 'chat.errors.exportFailed',
  transcript_not_ready: 'chat.errors.transcriptNotReady',
};

const CHAT_ERROR_CODES = new Set<string>(Object.keys(CHAT_ERROR_CODE_I18N_KEY));

/**
 * `chatStore`'s `error` field also carries a handful of camelCase values that
 * are already i18n key suffixes (`conversationNotFound`, `send`, ...) rather
 * than `ChatErrorCode` members — those pass through as `chat.errors.<value>`
 * unchanged, same as before this fix. Only real `ChatErrorCode` values go
 * through the snake_case → camelCase map above.
 */
export function resolveChatErrorI18nKey(error: string): string {
  return CHAT_ERROR_CODES.has(error)
    ? CHAT_ERROR_CODE_I18N_KEY[error as ChatErrorCode]
    : `chat.errors.${error}`;
}
