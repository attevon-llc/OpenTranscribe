/**
 * Settings section ids that no longer have a sidebar row of their own.
 *
 * `asr-provider`, `custom-vocabulary`, `engine-settings` and `speaker-attributes`
 * became tabs of the Transcription and Speaker Identification sections. Their ids
 * stay valid so settings search, deep links and fixtures keep working: opening one
 * selects its row and its tab.
 */
import type { SettingsSection } from '$stores/settingsModalStore';
import type { SpeakerIdTabId } from './speakerIdentificationTabs';
import type { TranscriptionTabId } from './transcriptionTabs';

export type AliasTarget =
  | { row: 'transcription'; tab: TranscriptionTabId }
  | { row: 'speaker-identification'; tab: SpeakerIdTabId };

export const SECTION_ALIASES: Record<
  'asr-provider' | 'custom-vocabulary' | 'engine-settings' | 'speaker-attributes',
  AliasTarget
> = {
  'asr-provider': { row: 'transcription', tab: 'tx-provider' },
  'custom-vocabulary': { row: 'transcription', tab: 'tx-vocabulary' },
  'engine-settings': { row: 'speaker-identification', tab: 'spk-engine' },
  'speaker-attributes': { row: 'speaker-identification', tab: 'spk-attributes' },
};

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

/** The tab an alias id opens inside its row, or null when the row's default applies. */
export function initialTabFor(section: SettingsSection): string | null {
  return tabAlias(section)?.tab ?? null;
}
