/**
 * settingsSearchIndex — build a searchable index of settings from the i18n tree.
 *
 * The settings panels are 100% translated via flat dot-notation i18n keys, so the
 * localized text is the natural search corpus: sourcing from the key tree means
 * search works in all 8 locales for free and automatically covers enumerated
 * option labels built with template-literal keys (which a component grep misses).
 *
 * This module is pure/testable. The caller supplies:
 *   - `dict`: the flat locale dictionary (i18next.getResourceBundle(lng,'translation'))
 *   - `visibleSections`: the sections the current user can actually open (already
 *     capability/edition-filtered by SettingsModal) with their resolved labels.
 * so the index never surfaces a section the user can't navigate to.
 */
import type { SettingsSection } from '$stores/settingsModalStore';
import { createFuzzyIndex, type FuzzyIndex } from './fuzzyMatcher';

export interface SettingsSearchItem {
  /** Section to navigate to when this result is chosen. */
  sectionId: SettingsSection;
  /** Parent section name (shown under the result, macOS style). */
  sectionLabel: string;
  /** The matched setting's display text. */
  label: string;
  /** Searchable text (label + any paired help/description). */
  keywords: string;
  /** Text used to locate + flash the control in the DOM after navigation. */
  anchorText: string;
  /** True for the row that represents the section itself. */
  isSectionTitle: boolean;
}

/**
 * i18n namespaces (full key prefixes) whose leaves belong to each section. Sub-panel
 * namespaces are folded into their parent section so their settings are findable and
 * navigation lands somewhere the user can actually reach. Seeded from SettingsModal's
 * `sidebarSections`; keep in sync when sections/panels are added.
 */
const SECTION_NAMESPACES: Partial<Record<SettingsSection, string[]>> = {
  'system-statistics': ['settings.statistics'],
  billing: ['billing'],
  usage: ['usage'],
  team: ['team'],
  'audit-logs': ['settings.auditLog'],
  authentication: [
    'settings.authentication',
    'settings.ldap',
    'settings.keycloak',
    'settings.pki',
    'settings.localAuth',
    'settings.session',
  ],
  'admin-users': ['settings.users', 'settings.accountStatus'],
  'data-integrity': ['settings.dataIntegrity'],
  retention: ['settings.retention', 'settings.cache'],
  backup: ['settings.backup'],
  'search-indexing': ['settings.searchIndexing', 'settings.search'],
  'embedding-migration': ['settings.embeddingMigration', 'settings.embeddingConsistency'],
  'admin-task-health': ['settings.taskHealth', 'settings.retry'],
  groups: ['groups'],
  profile: ['settings.profile', 'settings.security', 'settings.certificate', 'settings.language'],
  'ai-prompts': ['settings.aiPrompts', 'prompts'],
  'asr-provider': ['settings.asrProvider'],
  'engine-settings': ['settings.engineSettings'],
  'redaction-policy': ['settings.redactionPolicy'],
  'auto-labeling': ['autoLabel'],
  'custom-vocabulary': ['settings.customVocabulary', 'settings.vocabulary'],
  'content-redaction': ['settings.contentRedaction'],
  'llm-provider': ['settings.llmProvider', 'llm'],
  'organization-context': ['settings.orgContext'],
  'speaker-attributes': ['settings.speakerAttributes'],
  transcription: ['settings.transcription'],
  'audio-extraction': ['settings.audioExtraction'],
  'media-sources': ['settings.mediaSources'],
  'watch-sources': ['settings.watchSources', 'settings.emailNotifications'],
  recording: ['settings.recording'],
  download: ['settings.download'],
};

/**
 * Leaf tokens that are UI chrome (buttons/toasts/status), not real settings. The
 * last dotted segment of a key is checked against this set. Descriptions/help/hints
 * are intentionally NOT excluded — they are wanted in the index.
 */
const STOP_LEAVES = new Set(
  [
    'save',
    'saving',
    'saved',
    'saveFailed',
    'saveError',
    'saveSuccess',
    'saveChanges',
    'cancel',
    'close',
    'confirm',
    'confirming',
    'delete',
    'deleting',
    'deleted',
    'remove',
    'removing',
    'removed',
    'edit',
    'editing',
    'add',
    'adding',
    'added',
    'back',
    'retry',
    'retrying',
    'refresh',
    'refreshing',
    'reset',
    'resetting',
    'apply',
    'applying',
    'loading',
    'loadFailed',
    'loadError',
    'updating',
    'updated',
    'success',
    'error',
    'errorTitle',
    'testing',
    'testConnection',
    'testSuccess',
    'testFailed',
    'dismiss',
    'discard',
    'saveButton',
    'cancelButton',
  ].map((s) => s.toLowerCase())
);

/** Secondary text suffixes: these leaves are folded into their base setting as keywords. */
const SECONDARY_SUFFIX = /(Help|Hint|Description|Desc|Note|Tooltip|Subtitle|Caption|Placeholder)$/;

function cleanText(value: string): string {
  return value
    .replace(/\{\{[^}]*\}\}/g, '') // strip i18n interpolation placeholders
    .replace(/<[^>]*>/g, ' ') // strip any stray html
    .replace(/\s+/g, ' ')
    .trim();
}

function lastSegment(key: string): string {
  const idx = key.lastIndexOf('.');
  return idx === -1 ? key : key.slice(idx + 1);
}

function isStopLeaf(leaf: string): boolean {
  return STOP_LEAVES.has(leaf.toLowerCase());
}

export interface VisibleSection {
  id: SettingsSection;
  label: string;
}

/**
 * Build the flat list of searchable settings items for the given locale dictionary,
 * limited to the sections the user can open.
 */
export function buildSettingsSearchItems(
  dict: Record<string, string>,
  visibleSections: VisibleSection[]
): SettingsSearchItem[] {
  const items: SettingsSearchItem[] = [];
  if (!dict) return items;

  for (const section of visibleSections) {
    const sectionLabel = cleanText(section.label || '');
    const seen = new Set<string>();

    // The section itself is always findable by its own name.
    if (sectionLabel) {
      const key = sectionLabel.toLowerCase();
      seen.add(key);
      items.push({
        sectionId: section.id,
        sectionLabel,
        label: sectionLabel,
        keywords: sectionLabel,
        anchorText: sectionLabel,
        isSectionTitle: true,
      });
    }

    const namespaces = SECTION_NAMESPACES[section.id];
    if (!namespaces) continue;

    // Two passes so secondary (help/hint) text can attach to its base setting.
    const primaries = new Map<string, SettingsSearchItem>();
    const orphanSecondaries: Array<{ base: string; value: string }> = [];

    for (const ns of namespaces) {
      const prefix = `${ns}.`;
      for (const fullKey of Object.keys(dict)) {
        if (!fullKey.startsWith(prefix)) continue;
        if (fullKey.includes('.toast.') || fullKey.includes('.errors.')) continue;

        const leaf = lastSegment(fullKey);
        if (leaf === 'title' || leaf === 'description') {
          // Section-level title/description handled by the section-title row / kept minimal.
          // (A section's own title duplicates the section row; skip to avoid noise.)
          continue;
        }
        if (isStopLeaf(leaf)) continue;

        const value = cleanText(dict[fullKey] ?? '');
        if (!value || !/\p{L}/u.test(value)) continue;

        const relative = fullKey.slice(prefix.length);
        if (SECONDARY_SUFFIX.test(leaf)) {
          orphanSecondaries.push({ base: relative.replace(SECONDARY_SUFFIX, ''), value });
          continue;
        }

        const dedupeKey = value.toLowerCase();
        if (seen.has(dedupeKey)) continue;
        seen.add(dedupeKey);
        const item: SettingsSearchItem = {
          sectionId: section.id,
          sectionLabel,
          label: value,
          keywords: value,
          anchorText: value,
          isSectionTitle: false,
        };
        primaries.set(relative, item);
        items.push(item);
      }
    }

    // Attach secondary text to its base setting as extra keywords, or index it standalone.
    for (const secondary of orphanSecondaries) {
      const base = primaries.get(secondary.base);
      if (base) {
        base.keywords = `${base.keywords} ${secondary.value}`;
      } else {
        const dedupeKey = secondary.value.toLowerCase();
        if (seen.has(dedupeKey)) continue;
        seen.add(dedupeKey);
        items.push({
          sectionId: section.id,
          sectionLabel,
          label: secondary.value,
          keywords: secondary.value,
          anchorText: secondary.value,
          isSectionTitle: false,
        });
      }
    }
  }

  return items;
}

/** fuse.js field weights: a section's own name ranks highest, then the label, then help text. */
export function createSettingsFuzzyIndex(
  items: SettingsSearchItem[]
): FuzzyIndex<SettingsSearchItem> {
  return createFuzzyIndex(items, {
    keys: [
      { name: 'label', weight: 3 },
      { name: 'sectionLabel', weight: 2 },
      { name: 'keywords', weight: 1 },
    ],
    threshold: 0.4,
    minMatchCharLength: 2,
  });
}
