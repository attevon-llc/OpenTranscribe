/**
 * Explicit i18n key maps for values that come from a closed set or from the
 * backend's wire vocabulary (#970).
 *
 * Building a key by interpolating such a value into a template literal renders the
 * raw dotted key the moment the value has no string — and nothing fails: `check:i18n`
 * checks locale parity, not that a requested key exists. Every key here is a literal,
 * so `keyMaps.test.ts` can prove it resolves in every locale, and `Record<Union, …>`
 * makes the compiler reject a union member added without an entry.
 */
import type { ChatErrorCode, SearchMode } from '$lib/types/chat';
import type { DerivedSource } from '$lib/types/media';
import type { ContextWindowStatus, ReasoningOffSwitch } from '$lib/api/llmSettings';
import type { GrantableRole } from '$lib/api/groupMappings';
import type { WATCH_FILE_STATUSES } from '$lib/api/watchSourcesApi';

type KeyMap = Readonly<Record<string, string>>;
type Translate = (key: string, options?: Record<string, unknown>) => string;

const has = (map: object, value: string) => Object.prototype.hasOwnProperty.call(map, value);

/** The mapped key for `value`, or `fallbackKey` when the value is unknown/absent. */
export function keyFor(map: KeyMap, value: string | null | undefined, fallbackKey: string): string {
  return typeof value === 'string' && has(map, value) ? map[value] : fallbackKey;
}

/**
 * Translated label for `value`, or the raw value itself when the backend sent
 * something this build has no string for — an identifier beats a dotted key.
 */
export function labelFor(
  t: Translate,
  map: KeyMap,
  value: string | null | undefined,
  options?: Record<string, unknown>
): string {
  if (typeof value === 'string' && has(map, value)) return t(map[value], options);
  return value ?? '';
}

export const SEARCH_MODE_KEYS: Record<SearchMode, string> = {
  hybrid: 'chat.searchMode.hybrid',
  semantic: 'chat.searchMode.semantic',
  keyword: 'chat.searchMode.keyword',
};

export type ChatGroupKey = 'today' | 'yesterday' | 'last7Days' | 'last30Days' | 'older';
export const CHAT_GROUP_KEYS: Record<ChatGroupKey, string> = {
  today: 'chat.groups.today',
  yesterday: 'chat.groups.yesterday',
  last7Days: 'chat.groups.last7Days',
  last30Days: 'chat.groups.last30Days',
  older: 'chat.groups.older',
};

/** `state.error` in the chat store: a stream code, a store literal, or arbitrary text. */
export const CHAT_STATE_ERROR_KEYS: Record<
  ChatErrorCode | 'send' | 'conversationNotFound' | 'loadConversations' | 'loadMessages',
  string
> = {
  llm_unconfigured: 'chat.errors.llm_unconfigured',
  quota_exceeded: 'chat.errors.quota_exceeded',
  rate_limited: 'chat.errors.rate_limited',
  provider_error: 'chat.errors.provider_error',
  provider_unavailable: 'chat.errors.provider_unavailable',
  timeout: 'chat.errors.timeout',
  cancelled: 'chat.errors.cancelled',
  connection_interrupted: 'chat.errors.connection_interrupted',
  export_failed: 'chat.errors.export_failed',
  transcript_not_ready: 'chat.errors.transcript_not_ready',
  send: 'chat.errors.send',
  conversationNotFound: 'chat.errors.conversationNotFound',
  loadConversations: 'chat.errors.loadConversations',
  loadMessages: 'chat.errors.loadMessages',
};
export const CHAT_STATE_ERROR_FALLBACK = 'chat.message.errorGeneric';

export const PROVENANCE_SOURCE_KEYS: Record<DerivedSource | 'unresolved', string> = {
  container: 'provenance.source.container',
  filename: 'provenance.source.filename',
  transcript: 'provenance.source.transcript',
  llm: 'provenance.source.llm',
  manual: 'provenance.source.manual',
  none: 'provenance.source.none',
  unresolved: 'provenance.source.unresolved',
};

export const GROUP_MAPPING_ROLE_KEYS: Record<GrantableRole, string> = {
  user: 'settings.groupMappings.role.user',
  admin: 'settings.groupMappings.role.admin',
};

export const REASONING_OFF_SWITCH_KEYS: Record<ReasoningOffSwitch, string> = {
  unknown: 'settings.llmProvider.reasoningOffSwitch.unknown',
  unsupported: 'settings.llmProvider.reasoningOffSwitch.unsupported',
  no_reasoning: 'settings.llmProvider.reasoningOffSwitch.no_reasoning',
  absent: 'settings.llmProvider.reasoningOffSwitch.absent',
  works: 'settings.llmProvider.reasoningOffSwitch.works',
};

/** `measured` has no string of its own: its text depends on the relation (below). */
export const CONTEXT_WINDOW_STATUS_KEYS: Record<
  Exclude<ContextWindowStatus, 'measured'>,
  string
> = {
  unknown: 'settings.llmProvider.contextWindow.unknown',
  unsupported: 'settings.llmProvider.contextWindow.unsupported',
  not_found: 'settings.llmProvider.contextWindow.not_found',
  unreachable: 'settings.llmProvider.contextWindow.unreachable',
};

export const CONTEXT_WINDOW_RELATION_KEYS: Record<'below' | 'above' | 'match', string> = {
  below: 'settings.llmProvider.contextWindow.measured_below',
  above: 'settings.llmProvider.contextWindow.measured_above',
  match: 'settings.llmProvider.contextWindow.measured_match',
};

export const QUEUE_NAME_KEYS: Record<string, string> = {
  gpu: 'settings.statistics.queueGpu',
  download: 'settings.statistics.queueDownload',
  nlp: 'settings.statistics.queueNlp',
  embedding: 'settings.statistics.queueEmbedding',
  cpu: 'settings.statistics.queueCpu',
};

export const REDACTION_STYLE_KEYS: Record<string, string> = {
  label: 'settings.contentRedaction.styleOption.label',
  asterisks: 'settings.contentRedaction.styleOption.asterisks',
  first_letter: 'settings.contentRedaction.styleOption.first_letter',
  blur: 'settings.contentRedaction.styleOption.blur',
};

export const REDACTION_DETECTOR_KEYS: Record<string, string> = {
  profanity: 'settings.contentRedaction.detector.profanity',
  pii: 'settings.contentRedaction.detector.pii',
  toxicity: 'settings.contentRedaction.detector.toxicity',
  llm: 'settings.contentRedaction.detector.llm',
};

export const REDACTION_CATEGORY_KEYS: Record<string, string> = {
  profanity: 'settings.contentRedaction.category.profanity',
  pii: 'settings.contentRedaction.category.pii',
  toxicity: 'settings.contentRedaction.category.toxicity',
  custom: 'settings.contentRedaction.category.custom',
};

export const WATCH_FILE_STATUS_KEYS: Record<(typeof WATCH_FILE_STATUSES)[number], string> = {
  pending: 'settings.watchSources.files.status.pending',
  downloading: 'settings.watchSources.files.status.downloading',
  importing: 'settings.watchSources.files.status.importing',
  imported: 'settings.watchSources.files.status.imported',
  skipped_duplicate: 'settings.watchSources.files.status.skipped_duplicate',
  skipped_old: 'settings.watchSources.files.status.skipped_old',
  skipped_invalid: 'settings.watchSources.files.status.skipped_invalid',
  processing: 'settings.watchSources.files.status.processing',
  error: 'settings.watchSources.files.status.error',
  stitched_part: 'settings.watchSources.files.status.stitched_part',
  waiting_for_parts: 'settings.watchSources.files.status.waiting_for_parts',
};

export const ENRICHMENT_TASK_KEYS: Record<string, string> = {
  search_indexing: 'notifications.enrichment.search_indexing',
  analytics: 'notifications.enrichment.analytics',
  speaker_attributes: 'notifications.enrichment.speaker_attributes',
  speaker_identification: 'notifications.enrichment.speaker_identification',
  speaker_clustering: 'notifications.enrichment.speaker_clustering',
};

export const DURATION_CHIP_KEYS: Record<string, string> = {
  duration1to10: 'filter.duration1to10',
  duration11to30: 'filter.duration11to30',
  duration31to60: 'filter.duration31to60',
  duration60plus: 'filter.duration60plus',
};

export const SHARE_PERMISSION_KEYS: Record<string, string> = {
  viewer: 'sharing.permissionViewer',
  editor: 'sharing.permissionEditor',
  owner: 'sharing.permissionOwner',
};

export const ASR_TEST_STATUS_KEYS = {
  connected: 'settings.asrProvider.status.connected',
  failed: 'settings.asrProvider.status.failed',
} as const;
