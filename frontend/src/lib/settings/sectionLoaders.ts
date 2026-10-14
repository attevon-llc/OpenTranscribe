/**
 * Lazy loaders for the settings sections.
 *
 * The modal used to import every section statically, so its chunk (and the ~270 requests behind
 * it) had to land before the dialog could paint. Now each section's component is fetched with a
 * dynamic `import()` the first time it is shown (see `LazySection.svelte`).
 *
 * Imported by the modal chunk only, never by the root layout: see `settingsModalLoader.ts`.
 */
import type { SettingsSection } from '$stores/settingsModalStore';

/** A dynamic import of a Svelte component module. */
export type ComponentLoader = () => Promise<{ default: any }>;

export const sectionLoaders = {
  profile: () => import('$components/settings/UserProfileSettings.svelte'),
  audioExtraction: () => import('$components/settings/AudioExtractionSettings.svelte'),
  pipeline: () => import('$components/settings/PipelineOverview.svelte'),
  transcription: () => import('$components/settings/TranscriptionSection.svelte'),
  speakerIdentification: () => import('$components/settings/SpeakerIdentificationSection.svelte'),
  organizationContext: () => import('$components/settings/OrganizationContextSettings.svelte'),
  download: () => import('$components/settings/DownloadSettings.svelte'),
  mediaSources: () => import('$components/settings/MediaSourcesSettings.svelte'),
  watchSources: () => import('$components/settings/WatchSourcesSettings.svelte'),
  prompts: () => import('$components/settings/PromptSettings.svelte'),
  llm: () => import('$components/settings/LLMSettings.svelte'),
  autoLabel: () => import('$components/settings/AutoLabelSettings.svelte'),
  privacyRedaction: () => import('$components/settings/PrivacyRedactionSettings.svelte'),
  chat: () => import('$components/settings/ChatSettingsPanel.svelte'),
  searchIndexing: () => import('$components/settings/SearchSettings.svelte'),
  groups: () => import('$components/settings/GroupsSettings.svelte'),
  accountStatus: () => import('$components/settings/AccountStatusDashboard.svelte'),
  pendingApprovals: () => import('$components/settings/PendingApprovalsPanel.svelte'),
  userTable: () => import('$components/UserManagementTable.svelte'),
  lockedAccounts: () => import('$components/settings/LockedAccountsPanel.svelte'),
  quarantine: () => import('$components/settings/QuarantinePanel.svelte'),
  supportApprovals: () => import('$components/settings/SupportAccessApprovalsPanel.svelte'),
  supportStaff: () => import('$components/settings/SupportAccessStaffPanel.svelte'),
  statistics: () => import('$components/settings/SystemStatisticsPanel.svelte'),
  fileStatus: () => import('$components/settings/FileStatusSettings.svelte'),
  taskHealth: () => import('$components/settings/AdminTaskHealthPanel.svelte'),
  billing: () => import('$lib/cloud/components/BillingPanel.svelte'),
  usage: () => import('$lib/cloud/components/UsageDashboard.svelte'),
  team: () => import('$lib/cloud/components/TeamManagement.svelte'),
  embeddingMigration: () => import('$components/settings/EmbeddingMigrationSettings.svelte'),
  embeddingConsistency: () => import('$components/settings/EmbeddingConsistencySettings.svelte'),
  dataIntegrity: () => import('$components/settings/DataIntegritySettings.svelte'),
  retention: () => import('$components/settings/RetentionSettings.svelte'),
  backup: () => import('$components/settings/BackupSettings.svelte'),
  authentication: () => import('$components/settings/AuthenticationSettings.svelte'),
  auditLog: () => import('$components/settings/AuditLogViewer.svelte'),
} satisfies Record<string, ComponentLoader>;

export type SectionLoaderKey = keyof typeof sectionLoaders;

/** The loader whose component is the first thing each section shows (for prefetching). */
const LEAD_LOADER: Partial<Record<SettingsSection, SectionLoaderKey>> = {
  profile: 'profile',
  'audio-extraction': 'audioExtraction',
  'processing-pipeline': 'pipeline',
  transcription: 'transcription',
  'asr-provider': 'transcription',
  'custom-vocabulary': 'transcription',
  'speaker-identification': 'speakerIdentification',
  'speaker-attributes': 'speakerIdentification',
  'engine-settings': 'speakerIdentification',
  'organization-context': 'organizationContext',
  download: 'download',
  'media-sources': 'mediaSources',
  'watch-sources': 'watchSources',
  'ai-prompts': 'prompts',
  'llm-provider': 'llm',
  'auto-labeling': 'autoLabel',
  'content-redaction': 'privacyRedaction',
  'redaction-policy': 'privacyRedaction',
  chat: 'chat',
  'chat-admin': 'chat',
  'search-indexing': 'searchIndexing',
  groups: 'groups',
  'admin-users': 'userTable',
  quarantine: 'quarantine',
  'support-access-requests': 'supportApprovals',
  'support-access': 'supportStaff',
  'system-statistics': 'statistics',
  'file-status': 'fileStatus',
  'admin-task-health': 'taskHealth',
  billing: 'billing',
  usage: 'usage',
  team: 'team',
  'embedding-migration': 'embeddingMigration',
  'data-integrity': 'dataIntegrity',
  retention: 'retention',
  backup: 'backup',
  authentication: 'authentication',
  'audit-logs': 'auditLog',
};

/** Fetch the chunk behind a section's main component so its first display is instant. */
export function prefetchSection(section: SettingsSection): void {
  const key = LEAD_LOADER[section];
  if (key) void sectionLoaders[key]().catch(() => {});
}
