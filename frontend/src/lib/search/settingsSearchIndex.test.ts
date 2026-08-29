import { describe, it, expect } from 'vitest';
import {
  buildSettingsSearchItems,
  createSettingsFuzzyIndex,
  type VisibleSection,
} from './settingsSearchIndex';

const dict: Record<string, string> = {
  // engine-settings namespace
  'settings.engineSettings.title': 'Engine Configuration',
  'settings.engineSettings.diarizerBackend': 'Diarizer backend',
  'settings.engineSettings.diarizerBackendHelp': 'Choose the diarization engine to use',
  'settings.engineSettings.save': 'Save', // chrome — excluded
  'settings.engineSettings.saveFailed': 'Save failed', // chrome — excluded
  'settings.engineSettings.toast.saved': 'Saved!', // toast — excluded
  // authentication sub-panel (settings.ldap folds into authentication)
  'settings.authentication.title': 'Authentication',
  'settings.ldap.serverUrl': 'LDAP server URL',
  // content-redaction with an enumerated option label (dynamic key in components)
  'settings.contentRedaction.title': 'Content Redaction',
  'settings.contentRedaction.category.pii': 'Personal information',
  // a section NOT in the visible set — must be excluded
  'settings.backup.title': 'Backups',
  'settings.backup.retentionDays': 'Retention days',
};

const visible: VisibleSection[] = [
  { id: 'engine-settings', label: 'Engine Configuration' },
  { id: 'authentication', label: 'Authentication' },
  { id: 'content-redaction', label: 'Content Redaction' },
  // backup intentionally omitted (user can't see it)
];

describe('buildSettingsSearchItems', () => {
  const items = buildSettingsSearchItems(dict, visible);

  it('includes a section-title row for each visible section', () => {
    const titles = items.filter((i) => i.isSectionTitle).map((i) => i.label);
    expect(titles).toEqual(
      expect.arrayContaining(['Engine Configuration', 'Authentication', 'Content Redaction'])
    );
  });

  it('indexes real setting labels into the right section', () => {
    // `toBeTruthy()` on a `.find()` proved the row existed but nothing about it — a row
    // indexed under the wrong section is unreachable from the search results, which is the
    // failure this test is named for. Assert the row's identity.
    expect(items.find((i) => i.label === 'Diarizer backend')).toMatchObject({
      sectionId: 'engine-settings',
      sectionLabel: 'Engine Configuration',
      anchorText: 'Diarizer backend',
      isSectionTitle: false,
    });
    expect(items.find((i) => i.label === 'LDAP server URL')).toMatchObject({
      sectionId: 'authentication',
      isSectionTitle: false,
    });
  });

  it('folds settings.ldap.* into the authentication section', () => {
    const ldap = items.find((i) => i.label === 'LDAP server URL');
    expect(ldap?.sectionId).toBe('authentication');
  });

  it('excludes chrome leaves (save/saveFailed/toast)', () => {
    // Three `toBeFalsy()` assertions on `.find()` all pass when `items` is EMPTY, so this
    // test reported green for any bug that broke index building outright — the exact
    // vacuous-exclusion trap. Asserting the complete set of indexed settings proves the
    // exclusions AND that there is something to exclude from.
    const settingLabels = items.filter((i) => !i.isSectionTitle).map((i) => i.label);
    expect(settingLabels).toEqual(['Diarizer backend', 'LDAP server URL', 'Personal information']);
  });

  it('folds Help text into its base setting as keywords, not a separate row', () => {
    const rows = items.filter((i) => i.label === 'Choose the diarization engine to use');
    expect(rows.length).toBe(0);
    const backend = items.find((i) => i.label === 'Diarizer backend');
    expect(backend?.keywords).toContain('Choose the diarization engine to use');
  });

  it('includes enumerated option labels sourced from the key tree', () => {
    expect(items.find((i) => i.label === 'Personal information')).toMatchObject({
      sectionId: 'content-redaction',
      isSectionTitle: false,
    });
  });

  it('respects capability gating — hidden sections are not indexed', () => {
    // Positive control first: without it, both exclusions below pass on an empty index.
    expect(items.map((i) => i.sectionId)).toContain('engine-settings');
    expect(items.map((i) => i.sectionId)).not.toContain('backup');
    expect(items.map((i) => i.label)).not.toContain('Retention days');
    expect(items.map((i) => i.label)).not.toContain('Backups');
  });

  it('is empty for an empty dictionary', () => {
    expect(buildSettingsSearchItems({}, visible).some((i) => !i.isSectionTitle)).toBe(false);
  });
});

describe('createSettingsFuzzyIndex', () => {
  const items = buildSettingsSearchItems(dict, visible);
  const index = createSettingsFuzzyIndex(items);

  it('finds a setting and points at the right section', () => {
    const results = index.search('diarizer backend');
    expect(results[0].item.sectionId).toBe('engine-settings');
  });

  it('finds a setting via its help text (keywords)', () => {
    const results = index.search('diarization engine');
    expect(results.map((r) => r.item.sectionId)).toContain('engine-settings');
  });

  it('tolerates a typo', () => {
    const results = index.search('redaction');
    expect(results.map((r) => r.item.sectionId)).toContain('content-redaction');
  });
});
