/**
 * What the speaker save flows read and write on the file-detail page. The page keeps owning
 * the rendered state (#338); {@link SpeakerSaveContext} is the only way these modules touch it.
 */
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import type { SegmentBearingFile } from './segmentSync';
import type { SpeakerLoaderContext } from './speakerLoader';

export interface SpeakerSaveContext extends SpeakerLoaderContext {
  /** Must perform the page's real `file = ...` assignment (see `notificationHandler`). */
  setFile: (file: SegmentBearingFile) => void;
  loadSpeakers: () => Promise<void>;
  setSavingSpeakers: (saving: boolean) => void;
  /** Show (non-null) or close (null) the update-globally / create-new dialog. */
  setProfileConfirmation: (dialog: { title: string; message: string } | null) => void;
  closeSpeakerEditor: () => void;
  isDiarizationDisabled: () => boolean;
  refreshCaptionTrack: (reason: string) => void;
  refreshAnalytics: () => void;
}

/** Translate with the active locale, resolved at call time. */
export const tr = (key: string, options?: Record<string, unknown>) => get(t)(key, options);
