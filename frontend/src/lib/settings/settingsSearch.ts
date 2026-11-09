/**
 * Settings search plumbing for the modal: which sections are searchable for this user, the fuzzy
 * index over their localized labels, and the flash that shows where a hit landed.
 */
import i18next from 'i18next';
import { findSettingElement } from '$lib/search/findSettingElement';
import {
  buildSettingsSearchItems,
  createSettingsFuzzyIndex,
  type VisibleSection,
} from '$lib/search/settingsSearchIndex';
import {
  privacyRedactionTabs,
  type PrivacyRedactionAccess,
} from '$lib/settings/privacyRedactionTabs';
import { transcriptionTabs, type TranscriptionAccess } from '$lib/settings/transcriptionTabs';
import {
  speakerIdentificationTabs,
  type SpeakerIdAccess,
} from '$lib/settings/speakerIdentificationTabs';
import { dataManagementTabs, type DataAccess } from '$lib/settings/dataManagementTabs';
import { fileStatusTabs, type StatusAccess } from '$lib/settings/fileStatusTabs';

type Translate = (key: string, options?: Record<string, unknown>) => string;

export interface SearchableSidebarItem {
  id: string;
  label: string;
  locked: boolean;
}

export interface SearchAccess {
  isAdmin: boolean;
  isSuperAdmin: boolean;
  transcriptionAccess: TranscriptionAccess;
  speakerIdAccess: SpeakerIdAccess;
  dataAccess: DataAccess;
  statusAccess: StatusAccess;
  privacyAccess: PrivacyRedactionAccess;
}

function aliasSearchEntries(
  t: Translate,
  tx: TranscriptionAccess,
  spk: SpeakerIdAccess,
  data: DataAccess,
  status: StatusAccess
): VisibleSection[] {
  const dataOpen = (id: string) =>
    dataManagementTabs(data).some((tab) => tab.id === id && !tab.locked);
  const txOpen = (id: string) => transcriptionTabs(tx).some((tab) => tab.id === id && !tab.locked);
  const spkOpen = (id: string) =>
    speakerIdentificationTabs(spk).some((tab) => tab.id === id && !tab.locked);
  const entries: VisibleSection[] = [];
  if (fileStatusTabs(status).some((tab) => tab.id === 'status-recovery'))
    entries.push({
      id: 'admin-task-health',
      label: t('settings.taskHealth.title'),
    } as VisibleSection);
  if (txOpen('tx-provider'))
    entries.push({ id: 'asr-provider', label: t('settings.asrProvider.title') } as VisibleSection);
  if (txOpen('tx-vocabulary'))
    entries.push({
      id: 'custom-vocabulary',
      label: t('settings.customVocabulary.title'),
    } as VisibleSection);
  if (spkOpen('spk-attributes'))
    entries.push({
      id: 'speaker-attributes',
      label: t('settings.speakerAttributes.title'),
    } as VisibleSection);
  if (spkOpen('spk-engine'))
    entries.push({
      id: 'engine-settings',
      label: t('settings.engineSettings.title'),
    } as VisibleSection);
  if (dataOpen('data-retention'))
    entries.push({ id: 'retention', label: t('settings.retention.title') } as VisibleSection);
  if (dataOpen('data-backup'))
    entries.push({ id: 'backup', label: t('settings.backup.title') } as VisibleSection);
  if (dataOpen('data-integrity'))
    entries.push({
      id: 'data-integrity',
      label: t('settings.dataIntegrity.title'),
    } as VisibleSection);
  if (dataOpen('data-search'))
    entries.push({
      id: 'search-indexing',
      label: t('settings.searchIndexing.title'),
    } as VisibleSection);
  if (dataOpen('data-embeddings'))
    entries.push({
      id: 'embedding-migration',
      label: t('settings.embeddingMigration.title'),
    } as VisibleSection);
  return entries;
}

/**
 * Flat list of the sections this user can actually open: the search index is limited to these so
 * it never surfaces a section the user can't navigate to. Locked entries are excluded on purpose:
 * they stay discoverable in the sidebar (greyed out), but a search hit promises a usable
 * destination.
 */
export function buildVisibleSections(
  t: Translate,
  sidebar: { items: SearchableSidebarItem[] }[],
  access: SearchAccess
): VisibleSection[] {
  const rowOpen = (id: string) =>
    sidebar.some((section) => section.items.some((item) => item.id === id && !item.locked));
  return [
    ...sidebar.flatMap((section) =>
      section.items
        .filter((item) => !item.locked)
        .map((item) => ({ id: item.id, label: item.label }) as VisibleSection)
    ),
    // The policy tab has no sidebar row of its own, but its settings stay searchable
    // for exactly the users who can open it; the hit lands on that tab.
    ...(privacyRedactionTabs(access.privacyAccess).some((tab) => tab.id === 'policy' && !tab.locked)
      ? [{ id: 'redaction-policy', label: t('settings.redactionPolicy.title') } as VisibleSection]
      : []),
    // Same for the four ids that became tabs: no sidebar row, still searchable, and the
    // hit lands on the tab, but only for users whose tab is present and unlocked.
    ...aliasSearchEntries(
      t,
      access.transcriptionAccess,
      access.speakerIdAccess,
      access.dataAccess,
      access.statusAccess
    ),
    // The email-notifications tab is super_admin only and lives on the watch-sources row, so it
    // is searchable only where that row is shown and the user can open the tab.
    ...(access.isSuperAdmin && rowOpen('watch-sources')
      ? [
          {
            id: 'watch-sources-email',
            label: t('settings.emailNotifications.heading'),
          } as VisibleSection,
        ]
      : []),
    // The chat Advanced tab (platform-wide retrieval tuning) is admin only and lives on the
    // chat row; without this entry its settings were unsearchable and the "Advanced features"
    // hit opened the General tab.
    ...(access.isAdmin && rowOpen('chat')
      ? [{ id: 'chat-admin', label: t('chat.settings.tabs.advanced') } as VisibleSection]
      : []),
  ];
}

/** The fuzzy index over the localized setting labels, or null before i18next is ready. */
export function buildSettingsFuzzyIndex(sections: VisibleSection[]) {
  if (!i18next.isInitialized) return null;
  const dict = i18next.getResourceBundle(i18next.language, 'translation') as Record<
    string,
    string
  > | null;
  return createSettingsFuzzyIndex(buildSettingsSearchItems(dict || {}, sections));
}

/** Scroll the control a search hit points at into view and flash it. */
export function flashSettingControl(
  modalElement: HTMLElement | undefined,
  anchorText: string,
  attempt = 0
) {
  if (!anchorText) return;
  const container = modalElement?.querySelector('.settings-content');
  const el = container ? findSettingElement(container, anchorText) : null;
  if (!el) {
    // Panel content may still be loading — retry briefly (~1s total) before giving up.
    if (attempt < 20) {
      window.setTimeout(() => flashSettingControl(modalElement, anchorText, attempt + 1), 50);
    }
    return;
  }
  const scrollTarget =
    (el.closest(
      '.form-group, .form-row, .form-field, .settings-section, .content-section'
    ) as HTMLElement) || el;
  scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'center' });
  document
    .querySelectorAll('.settings-search-flash')
    .forEach((node) => node.classList.remove('settings-search-flash'));
  scrollTarget.classList.add('settings-search-flash');
  window.setTimeout(() => scrollTarget.classList.remove('settings-search-flash'), 1600);
}
