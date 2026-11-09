import type { AudioExtractionSettings } from '$lib/api/audioExtractionSettings';
import { get } from 'svelte/store';
import { capabilities, isCapabilityEnabled } from '$stores/capabilities';
import { getAudioExtractionSettings } from '$lib/api/audioExtractionSettings';
import { ASRSettingsApi } from '$lib/api/asrSettings';
import {
  getTranscriptionSettings,
  getTranscriptionSystemDefaults,
  DEFAULT_TRANSCRIPTION_SETTINGS,
  type TranscriptionSettings,
  type TranscriptionSystemDefaults,
} from '$lib/api/transcriptionSettings';
import { applySkippedSteps, type StepConfig } from './wizardSteps';

const PREVIOUS_VALUES_KEY = 'opentr:uploadPreviousValues';
const RECORDING_SETTINGS_KEY = 'recordingSettings';

export interface UploadPreviousValues {
  collectionIds: string[];
  collectionNames: string[];
  tagNames: string[];
  minSpeakers: number | null;
  maxSpeakers: number | null;
  numSpeakers: number | null;
  skipSummary: boolean;
  selectedWhisperModel: string | null;
  skippedSteps: string[]; // step IDs that were skipped (e.g. ['tags', 'collections'])
  timestamp: number;
}

type NamedCollection = { uuid: string; name: string };

/** What the wizard restores from the last submit; every field is optional. */
export interface RestoredPreferences {
  tags?: string[];
  collections?: NamedCollection[];
  minSpeakers?: number | null;
  maxSpeakers?: number | null;
  numSpeakers?: number | null;
  skipSummary?: boolean;
  selectedWhisperModel?: string | null;
  steps?: StepConfig[];
}

/**
 * Read the previously submitted choices and translate them into component state.
 * Returns null when nothing is stored or it cannot be parsed.
 */
export function restorePreviousValues(
  availableCollections: NamedCollection[],
  steps: StepConfig[]
): RestoredPreferences | null {
  try {
    const stored = localStorage.getItem(PREVIOUS_VALUES_KEY);
    if (!stored) return null;
    const prev: UploadPreviousValues = JSON.parse(stored);
    const out: RestoredPreferences = {};

    if (prev.tagNames?.length > 0) out.tags = [...prev.tagNames];

    // Validate that the remembered collections still exist
    if (prev.collectionIds?.length > 0) {
      const valid = prev.collectionIds
        .map((id) => availableCollections.find((c) => c.uuid === id))
        .filter(Boolean) as NamedCollection[];
      if (valid.length > 0) out.collections = valid;
    }

    if (prev.minSpeakers !== undefined) out.minSpeakers = prev.minSpeakers;
    if (prev.maxSpeakers !== undefined) out.maxSpeakers = prev.maxSpeakers;
    if (prev.numSpeakers !== undefined) out.numSpeakers = prev.numSpeakers;
    if (prev.skipSummary) out.skipSummary = prev.skipSummary;
    if (prev.selectedWhisperModel !== undefined) {
      out.selectedWhisperModel = prev.selectedWhisperModel;
    }
    if (prev.skippedSteps?.length > 0) {
      out.steps = applySkippedSteps(steps, prev.skippedSteps);
    }
    return out;
  } catch {
    return null; // ignore parse errors
  }
}

export function savePreviousValues(values: UploadPreviousValues): void {
  localStorage.setItem(PREVIOUS_VALUES_KEY, JSON.stringify(values));
}

export interface RecordingSettings {
  maxRecordingDuration: number;
  recordingQuality: string;
  autoStopEnabled: boolean;
}

const DEFAULT_RECORDING_SETTINGS: RecordingSettings = {
  maxRecordingDuration: 7200,
  recordingQuality: 'high',
  autoStopEnabled: true,
};

/** Recording preferences from localStorage (duration is stored in minutes, returned in seconds). */
export function loadRecordingSettings(): RecordingSettings {
  const settings = localStorage.getItem(RECORDING_SETTINGS_KEY);
  if (!settings) return { ...DEFAULT_RECORDING_SETTINGS };
  try {
    const parsed = JSON.parse(settings);
    return {
      maxRecordingDuration: (parsed.maxRecordingDuration || 120) * 60,
      recordingQuality: parsed.recordingQuality || 'high',
      autoStopEnabled: parsed.autoStopEnabled !== undefined ? parsed.autoStopEnabled : true,
    };
  } catch {
    return { ...DEFAULT_RECORDING_SETTINGS };
  }
}

const FALLBACK_AUDIO_EXTRACTION_SETTINGS: AudioExtractionSettings = {
  auto_extract_enabled: true,
  extraction_threshold_mb: 100,
  remember_choice: false,
  show_modal: true,
};

/** Used when the system-defaults endpoint cannot be reached. */
const FALLBACK_TRANSCRIPTION_SYSTEM_DEFAULTS: TranscriptionSystemDefaults = {
  min_speakers: 1,
  max_speakers: 20,
  garbage_cleanup_enabled: true,
  garbage_cleanup_threshold: 50,
  valid_speaker_prompt_behaviors: ['always_prompt', 'use_defaults', 'use_custom'],
  available_source_languages: { auto: 'Auto-detect', en: 'English' },
  available_llm_output_languages: { en: 'English' },
  common_languages: ['auto', 'en'],
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
  hallucination_silence_threshold: null,
  repetition_penalty: 1.0,
  diarization_source_default: 'provider',
  valid_diarization_sources: ['provider', 'local', 'pyannote', 'off'],
};

const DEFAULT_ADMIN_MODEL = 'large-v3-turbo';

/** The user's audio-extraction preferences, or the built-in defaults when they cannot be had. */
export async function loadAudioExtractionSettings(): Promise<AudioExtractionSettings> {
  // Off = the deployment owns these preferences and the endpoint does not exist.
  if (!isCapabilityEnabled(get(capabilities), 'audio_extraction')) {
    return FALLBACK_AUDIO_EXTRACTION_SETTINGS;
  }
  try {
    return await getAudioExtractionSettings();
  } catch {
    return FALLBACK_AUDIO_EXTRACTION_SETTINGS;
  }
}

/** User transcription settings plus system defaults; both fall back together on any failure. */
export async function loadTranscriptionPreferences(): Promise<{
  settings: TranscriptionSettings;
  systemDefaults: TranscriptionSystemDefaults;
}> {
  try {
    const [settings, systemDefaults] = await Promise.all([
      getTranscriptionSettings(),
      getTranscriptionSystemDefaults(),
    ]);
    return { settings, systemDefaults };
  } catch {
    return {
      settings: { ...DEFAULT_TRANSCRIPTION_SETTINGS },
      systemDefaults: FALLBACK_TRANSCRIPTION_SYSTEM_DEFAULTS,
    };
  }
}

/** The model the admin pinned for local transcription. */
export async function loadAdminDefaultModel(): Promise<string> {
  try {
    const activeInfo = await ASRSettingsApi.getActiveLocalModel();
    return activeInfo.active_model || DEFAULT_ADMIN_MODEL;
  } catch {
    return DEFAULT_ADMIN_MODEL;
  }
}
