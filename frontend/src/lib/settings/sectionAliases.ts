/**
 * Settings section ids that no longer have a sidebar row of their own.
 *
 * `asr-provider`, `custom-vocabulary`, `engine-settings` and `speaker-attributes`
 * became tabs of the Transcription and Speaker Identification sections; `retention`, `backup`,
 * `data-integrity`, `search-indexing` and `embedding-migration` became tabs of Data & Storage; `admin-task-health` became the Recovery tab of File status. Their ids
 * stay valid so settings search, deep links and fixtures keep working: opening one
 * selects its row and its tab.
 */
import type { SettingsSection } from '$stores/settingsModalStore';
import type { DataTabId } from './dataManagementTabs';
import type { StatusTabId } from './fileStatusTabs';
import type { SpeakerIdTabId } from './speakerIdentificationTabs';
import type { TranscriptionTabId } from './transcriptionTabs';
import type { WatchSourcesTabId } from './watchSourcesTabs';

export type AliasTarget =
  | { row: 'transcription'; tab: TranscriptionTabId }
  | { row: 'speaker-identification'; tab: SpeakerIdTabId }
  | { row: 'data-management'; tab: DataTabId }
  | { row: 'file-status'; tab: StatusTabId }
  | { row: 'watch-sources'; tab: WatchSourcesTabId };

export const SECTION_ALIASES: Record<
  | 'asr-provider'
  | 'custom-vocabulary'
  | 'engine-settings'
  | 'speaker-attributes'
  | 'retention'
  | 'backup'
  | 'data-integrity'
  | 'search-indexing'
  | 'embedding-migration'
  | 'admin-task-health'
  | 'watch-sources-email'
  | 'watch-sources-global',
  AliasTarget
> = {
  'asr-provider': { row: 'transcription', tab: 'tx-provider' },
  'custom-vocabulary': { row: 'transcription', tab: 'tx-vocabulary' },
  'engine-settings': { row: 'speaker-identification', tab: 'spk-engine' },
  'speaker-attributes': { row: 'speaker-identification', tab: 'spk-attributes' },
  retention: { row: 'data-management', tab: 'data-retention' },
  backup: { row: 'data-management', tab: 'data-backup' },
  'data-integrity': { row: 'data-management', tab: 'data-integrity' },
  'search-indexing': { row: 'data-management', tab: 'data-search' },
  'embedding-migration': { row: 'data-management', tab: 'data-embeddings' },
  'admin-task-health': { row: 'file-status', tab: 'status-recovery' },
  'watch-sources-email': { row: 'watch-sources', tab: 'email' },
  'watch-sources-global': { row: 'watch-sources', tab: 'global' },
};

/** The ids that open a tab of a row, so the row can show their unsaved-changes dot. */
export function aliasIdsOf(row: SettingsSection): SettingsSection[] {
  return (Object.keys(SECTION_ALIASES) as (keyof typeof SECTION_ALIASES)[]).filter(
    (id) => SECTION_ALIASES[id].row === row
  );
}

// Sections that fold into another section without a tab of their own.
const ROW_ONLY_ALIASES: Partial<Record<SettingsSection, SettingsSection>> = {
  'redaction-policy': 'content-redaction',
  'chat-admin': 'chat',
};

function tabAlias(section: SettingsSection): AliasTarget | undefined {
  return Object.prototype.hasOwnProperty.call(SECTION_ALIASES, section)
    ? SECTION_ALIASES[section as keyof typeof SECTION_ALIASES]
    : undefined;
}

/** The sidebar row that should look active for a section id. */
export function sidebarRowFor(section: SettingsSection): SettingsSection {
  return tabAlias(section)?.row ?? ROW_ONLY_ALIASES[section] ?? section;
}

/** The Transcription tab an alias id opens, or null when the row's default applies. */
export function transcriptionTabFor(section: SettingsSection): TranscriptionTabId | null {
  const target = tabAlias(section);
  return target?.row === 'transcription' ? target.tab : null;
}

/** The Speaker Identification tab an alias id opens, or null when the row's default applies. */
export function speakerIdTabFor(section: SettingsSection): SpeakerIdTabId | null {
  const target = tabAlias(section);
  return target?.row === 'speaker-identification' ? target.tab : null;
}

/** The Data & Storage tab an alias id opens, or null when the row's default applies. */
export function dataTabFor(section: SettingsSection): DataTabId | null {
  const target = tabAlias(section);
  return target?.row === 'data-management' ? target.tab : null;
}

/** The File status tab an alias id opens, or null when the row's default applies. */
export function statusTabFor(section: SettingsSection): StatusTabId | null {
  const target = tabAlias(section);
  return target?.row === 'file-status' ? target.tab : null;
}

/** The Watch Sources tab an alias id opens, or null when the row's default applies. */
export function watchSourcesTabFor(section: SettingsSection): WatchSourcesTabId | null {
  const target = tabAlias(section);
  return target?.row === 'watch-sources' ? target.tab : null;
}
