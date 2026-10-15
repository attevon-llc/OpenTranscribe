/**
 * Which tabs the "Transcription" settings section offers.
 *
 * Transcription means turning speech into words: language, the ASR provider and
 * model, vocabulary, and accuracy tuning. Who spoke when lives in the separate
 * Speaker Identification section (`speakerIdentificationTabs`).
 *
 * No tab is role-locked: every endpoint behind them is user-tier. The super-admin
 * parts of the provider tab (pinning the local model, restarting the worker) are
 * locked inside that panel instead. A tab is absent only when the deployment lacks
 * its capability.
 */

export type TranscriptionTabId = 'tx-language' | 'tx-provider' | 'tx-vocabulary' | 'tx-accuracy';

export interface TranscriptionTab {
  id: TranscriptionTabId;
  locked: boolean;
}

export interface TranscriptionAccess {
  /** `transcription.prefs` capability. */
  prefsCap: boolean;
  /** `asr.user_providers` capability. */
  asrCap: boolean;
  /** `vocab.user` capability. */
  vocabCap: boolean;
}

export function transcriptionTabs(access: TranscriptionAccess): TranscriptionTab[] {
  const tabs: TranscriptionTab[] = [];
  if (access.prefsCap) tabs.push({ id: 'tx-language', locked: false });
  if (access.asrCap) tabs.push({ id: 'tx-provider', locked: false });
  if (access.vocabCap) tabs.push({ id: 'tx-vocabulary', locked: false });
  if (access.prefsCap) tabs.push({ id: 'tx-accuracy', locked: false });
  return tabs;
}

/** Whether the sidebar should list the section at all. */
export function transcriptionVisible(access: TranscriptionAccess): boolean {
  return transcriptionTabs(access).length > 0;
}

/**
 * The tab to show for a requested one: the request when it is openable, otherwise
 * the first openable tab, otherwise (every tab locked) the first tab so the pane
 * can explain the lock instead of rendering blank.
 */
export function resolveTranscriptionTab(
  requested: TranscriptionTabId,
  tabs: TranscriptionTab[]
): TranscriptionTabId | null {
  const wanted = tabs.find((tab) => tab.id === requested && !tab.locked);
  if (wanted) return wanted.id;
  return (tabs.find((tab) => !tab.locked) ?? tabs[0])?.id ?? null;
}
