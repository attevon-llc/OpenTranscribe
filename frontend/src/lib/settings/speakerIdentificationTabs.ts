/**
 * Which tabs the "Speaker Identification" settings section offers.
 *
 * Speaker identification means working out who spoke when: separating the voices,
 * the expected speaker count, voice attributes, and the deployment-wide engine.
 * It never changes the transcribed words (that is `transcriptionTabs`).
 *
 * A tab the user's tier cannot open is returned `locked` rather than dropped,
 * matching the Settings convention for a privilege the user merely lacks. The
 * engine and maintenance tabs are absent for a plain user, who could never reach
 * them (today's sidebar hides the engine row from non-admins).
 */

export type SpeakerIdTabId = 'spk-detection' | 'spk-attributes' | 'spk-engine' | 'spk-maintenance';

export interface SpeakerIdTab {
  id: SpeakerIdTabId;
  locked: boolean;
}

export interface SpeakerIdAccess {
  isAdmin: boolean;
  isSuperAdmin: boolean;
  /** `transcription.prefs` capability (the speaker card shares its endpoint). */
  prefsCap: boolean;
  /** `engine.settings` capability. */
  engineCap: boolean;
  /** `speaker_attributes.migration` capability. */
  migrationCap: boolean;
}

export function speakerIdentificationTabs(access: SpeakerIdAccess): SpeakerIdTab[] {
  const tabs: SpeakerIdTab[] = [];
  if (access.prefsCap) tabs.push({ id: 'spk-detection', locked: false });
  tabs.push({ id: 'spk-attributes', locked: false });
  if (access.isAdmin && access.engineCap) {
    tabs.push({ id: 'spk-engine', locked: !access.isSuperAdmin });
  }
  if (access.isAdmin && access.migrationCap) {
    tabs.push({ id: 'spk-maintenance', locked: !access.isSuperAdmin });
  }
  return tabs;
}

/** Whether the sidebar should list the section at all. */
export function speakerIdentificationVisible(access: SpeakerIdAccess): boolean {
  return speakerIdentificationTabs(access).length > 0;
}

/**
 * The tab to show for a requested one: the request when it is openable, otherwise
 * the first openable tab, otherwise (every tab locked) the first tab so the pane
 * can explain the lock instead of rendering blank.
 */
export function resolveSpeakerIdTab(
  requested: SpeakerIdTabId,
  tabs: SpeakerIdTab[]
): SpeakerIdTabId | null {
  const wanted = tabs.find((tab) => tab.id === requested && !tab.locked);
  if (wanted) return wanted.id;
  return (tabs.find((tab) => !tab.locked) ?? tabs[0])?.id ?? null;
}
