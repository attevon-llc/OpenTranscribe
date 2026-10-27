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
import { runIdleQueue } from '$lib/settings/idleQueue';
import { hasSectionPrefetch, prefetchSectionData } from '$lib/settings/sectionData';

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
  orgTakedown: () => import('$components/settings/QuarantinePanel.svelte'),
  orgAuditLog: () => import('$components/settings/OrgAuditLogPanel.svelte'),
  orgHolds: () => import('$components/settings/OrgLegalHoldsPanel.svelte'),
  takedownNotices: () => import('$components/settings/TakedownNoticesPanel.svelte'),
  myTakedowns: () => import('$components/settings/MyTakedownsPanel.svelte'),
  supportApprovals: () => import('$components/settings/SupportAccessApprovalsPanel.svelte'),
  supportStaff: () => import('$components/settings/SupportAccessStaffPanel.svelte'),
  legal: () => import('$components/settings/LegalSettings.svelte'),
  statistics: () => import('$components/settings/SystemStatisticsPanel.svelte'),
  fileStatusSection: () => import('$components/settings/FileStatusSection.svelte'),
  fileStatus: () => import('$components/settings/FileStatusSettings.svelte'),
  taskHealth: () => import('$components/settings/AdminTaskHealthPanel.svelte'),
  billing: () => import('$lib/cloud/components/BillingPanel.svelte'),
  usage: () => import('$lib/cloud/components/UsageDashboard.svelte'),
  team: () => import('$lib/cloud/components/TeamManagement.svelte'),
  embeddingMigration: () => import('$components/settings/EmbeddingMigrationSettings.svelte'),
  embeddingConsistency: () => import('$components/settings/EmbeddingConsistencySettings.svelte'),
  dataIntegrity: () => import('$components/settings/DataIntegritySettings.svelte'),
  dataManagement: () => import('$components/settings/DataManagementSection.svelte'),
  retention: () => import('$components/settings/RetentionSettings.svelte'),
  cache: () => import('$components/settings/CacheSettings.svelte'),
  backup: () => import('$components/settings/BackupSettings.svelte'),
  // The shell opens on its Local tab; start that chunk with the shell instead of after it mounts,
  // so the two arrive together rather than one round trip apart.
  authentication: () => {
    void authTabLoaders.local().catch(() => {});
    return import('$components/settings/AuthenticationSettings.svelte');
  },
  auditLog: () => import('$components/settings/AuditLogViewer.svelte'),
} satisfies Record<string, ComponentLoader>;

/**
 * The Authentication tab panels. Each is fetched when its tab opens, so the section no longer
 * pulls every IdP form (and their shared modules) before showing the first one.
 */
export const authTabLoaders = {
  local: () => import('$components/settings/LocalAuthSettings.svelte'),
  ldap: () => import('$components/settings/LDAPSettings.svelte'),
  oidc: () => import('$components/settings/OIDCSettings.svelte'),
  pki: () => import('$components/settings/PKISettings.svelte'),
  proxy: () => import('$components/settings/ProxySettings.svelte'),
  saml: () => import('$components/settings/SAMLSettings.svelte'),
  mappings: () => import('$components/settings/GroupMappingSettings.svelte'),
  scim: () => import('$components/settings/SCIMTokenSettings.svelte'),
  directorySync: () => import('$components/settings/DirectorySyncSettings.svelte'),
  session: () => import('$components/settings/SessionSettings.svelte'),
  mail: () => import('$components/settings/AuthMailDesignation.svelte'),
  audit: () => import('$components/settings/AuthConfigAuditPanel.svelte'),
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
  'watch-sources-email': 'watchSources',
  'watch-sources-global': 'watchSources',
  'ai-prompts': 'prompts',
  'llm-provider': 'llm',
  'auto-labeling': 'autoLabel',
  'content-redaction': 'privacyRedaction',
  'redaction-policy': 'privacyRedaction',
  chat: 'chat',
  'chat-admin': 'chat',
  groups: 'groups',
  'admin-users': 'userTable',
  quarantine: 'quarantine',
  'takedown-notices': 'takedownNotices',
  'my-takedowns': 'myTakedowns',
  'support-access-requests': 'supportApprovals',
  'support-access': 'supportStaff',
  'system-statistics': 'statistics',
  // File status: the tab shell is the lead chunk; admin-task-health opens its Recovery tab.
  legal: 'legal',
  'file-status': 'fileStatusSection',
  'admin-task-health': 'fileStatusSection',
  billing: 'billing',
  usage: 'usage',
  team: 'team',
  'org-takedown': 'orgTakedown',
  'org-audit-logs': 'orgAuditLog',
  'org-holds': 'orgHolds',
  // Data & Storage: the shell is the lead chunk; its first tab loads on arrival.
  'data-management': 'dataManagement',
  'data-integrity': 'dataManagement',
  retention: 'dataManagement',
  backup: 'dataManagement',
  'search-indexing': 'dataManagement',
  'embedding-migration': 'dataManagement',
  authentication: 'authentication',
  'audit-logs': 'auditLog',
};

/** Fetch the chunk behind a section's main component so its first display is instant. */
export function prefetchSection(section: SettingsSection): void {
  const key = LEAD_LOADER[section];
  if (key) void sectionLoaders[key]().catch(() => {});
}

export type PrefetchRole = 'user' | 'admin' | 'super_admin';

/**
 * Hover / keyboard-focus intent on a sidebar row: warm that section's code and, where it has a
 * registered cheap read, its data (role-checked), so the click that follows paints from memory.
 */
export function prefetchSectionIntent(section: SettingsSection, role: PrefetchRole): void {
  prefetchSection(section);
  prefetchSectionData(section, role);
}

/** Background work is skipped on Data Saver and on connections too slow to spare the bytes. */
export function connectionAllowsPrefetch(): boolean {
  const connection =
    typeof navigator === 'undefined'
      ? undefined
      : (
          navigator as Navigator & {
            connection?: { saveData?: boolean; effectiveType?: string };
          }
        ).connection;
  if (!connection) return true;
  if (connection.saveData) return false;
  return connection.effectiveType !== 'slow-2g' && connection.effectiveType !== '2g';
}

const PREFETCH_CONCURRENCY = 2;

/**
 * Warm every other section's code (then the data behind the few that have a cheap read) while the
 * browser is idle, so clicking through the sidebar finds each chunk already in memory.
 *
 * `sections` must already be the ones this user can open (the caller passes the unlocked sidebar
 * rows): nothing is fetched on behalf of a section the user has no rights to. Nothing starts on
 * Data Saver / 2g. Returns a cancel function for when the modal closes.
 */
export function startIdlePrefetch(
  sections: SettingsSection[],
  role: PrefetchRole,
  skip: SettingsSection | null = null
): () => void {
  if (!connectionAllowsPrefetch()) return () => {};

  const seen = new Set<SectionLoaderKey>();
  const tasks: Array<() => Promise<unknown> | void> = [];
  for (const section of sections) {
    if (section === skip) continue;
    const key = LEAD_LOADER[section];
    if (!key || seen.has(key)) continue;
    seen.add(key);
    tasks.push(() => sectionLoaders[key]());
  }
  // Data last: a few single small GETs, each only if this role may call it.
  for (const section of sections) {
    if (hasSectionPrefetch(section, role)) tasks.push(() => prefetchSectionData(section, role));
  }
  return runIdleQueue(tasks, PREFETCH_CONCURRENCY);
}
