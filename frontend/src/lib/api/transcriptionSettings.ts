/**
 * API service for transcription user settings
 *
 * Manages user preferences for transcription behavior including:
 * - Speaker detection settings (min/max speakers)
 * - Prompt behavior (always_prompt, use_defaults, use_custom)
 * - Garbage segment cleanup settings
 */

import axiosInstance from '../axios';

/**
 * Valid speaker prompt behavior options
 * - always_prompt: Always show Advanced Settings expanded on upload
 * - use_defaults: Use system defaults (MIN/MAX_SPEAKERS env vars), hide settings
 * - use_custom: Use user's saved min/max values, hide settings
 */
export type SpeakerPromptBehavior = 'always_prompt' | 'use_defaults' | 'use_custom';

/**
 * Valid diarization source options
 * - provider: Use ASR provider's built-in diarization
 * - local: Always use local PyAnnote on GPU
 * - pyannote: Use pyannote.ai cloud diarization
 * - off: No diarization
 */
export type DiarizationSource = 'provider' | 'local' | 'pyannote' | 'off';

/**
 * Language option with code and display name
 */
export interface LanguageOption {
  code: string;
  name: string;
}

/**
 * System-level transcription defaults from environment configuration
 */
export interface TranscriptionSystemDefaults {
  min_speakers: number;
  max_speakers: number;
  garbage_cleanup_enabled: boolean;
  garbage_cleanup_threshold: number;
  valid_speaker_prompt_behaviors: SpeakerPromptBehavior[];
  available_source_languages: Record<string, string>;
  available_llm_output_languages: Record<string, string>;
  common_languages: string[];
  vad_threshold: number;
  vad_min_silence_ms: number;
  vad_min_speech_ms: number;
  vad_speech_pad_ms: number;
  hallucination_silence_threshold: number | null;
  repetition_penalty: number;
  diarization_source_default: DiarizationSource;
  valid_diarization_sources: DiarizationSource[];
}

/**
 * User transcription settings
 */
export interface TranscriptionSettings {
  min_speakers: number;
  max_speakers: number;
  speaker_prompt_behavior: SpeakerPromptBehavior;
  garbage_cleanup_enabled: boolean;
  garbage_cleanup_threshold: number;
  source_language: string;
  translate_to_english: boolean;
  llm_output_language: string;
  vad_threshold: number;
  vad_min_silence_ms: number;
  vad_min_speech_ms: number;
  vad_speech_pad_ms: number;
  hallucination_silence_threshold: number | null;
  repetition_penalty: number;
  diarization_source: DiarizationSource;
}

/**
 * Request payload for updating transcription settings
 */
export interface TranscriptionSettingsUpdate {
  min_speakers?: number;
  max_speakers?: number;
  speaker_prompt_behavior?: SpeakerPromptBehavior;
  garbage_cleanup_enabled?: boolean;
  garbage_cleanup_threshold?: number;
  source_language?: string;
  translate_to_english?: boolean;
  llm_output_language?: string;
  vad_threshold?: number;
  vad_min_silence_ms?: number;
  vad_min_speech_ms?: number;
  vad_speech_pad_ms?: number;
  hallucination_silence_threshold?: number | null;
  repetition_penalty?: number;
  diarization_source?: DiarizationSource;
}

/**
 * Response from reset endpoint
 */
export interface TranscriptionSettingsResetResponse {
  message: string;
  default_settings: TranscriptionSettings;
}

/**
 * Default transcription settings (client-side fallback)
 * These should match the backend defaults
 */
export const DEFAULT_TRANSCRIPTION_SETTINGS: TranscriptionSettings = {
  min_speakers: 1,
  max_speakers: 20,
  speaker_prompt_behavior: 'always_prompt',
  garbage_cleanup_enabled: true,
  garbage_cleanup_threshold: 50,
  source_language: 'auto',
  translate_to_english: false,
  llm_output_language: 'en',
  vad_threshold: 0.5,
  vad_min_silence_ms: 2000,
  vad_min_speech_ms: 250,
  vad_speech_pad_ms: 400,
  hallucination_silence_threshold: null,
  repetition_penalty: 1.0,
  diarization_source: 'provider',
};

/**
 * Get user's transcription settings
 */
export async function getTranscriptionSettings(): Promise<TranscriptionSettings> {
  const response = await axiosInstance.get('/user-settings/transcription');
  return response.data;
}

/**
 * Update user's transcription settings
 */
export async function updateTranscriptionSettings(
  settings: TranscriptionSettingsUpdate
): Promise<TranscriptionSettings> {
  const response = await axiosInstance.put('/user-settings/transcription', settings);
  return response.data;
}

/** The field groups the backend can reset independently (`?group=`). */
export type TranscriptionSettingsGroup = 'language' | 'accuracy' | 'speakers';

/**
 * Reset transcription settings to system defaults. With a group, only that
 * group's fields are reset; without one, every field is.
 */
export async function resetTranscriptionSettings(
  group?: TranscriptionSettingsGroup
): Promise<TranscriptionSettingsResetResponse> {
  const response = group
    ? await axiosInstance.delete('/user-settings/transcription', { params: { group } })
    : await axiosInstance.delete('/user-settings/transcription');
  return response.data;
}

/**
 * Get system default values for transcription settings
 */
export async function getTranscriptionSystemDefaults(): Promise<TranscriptionSystemDefaults> {
  const response = await axiosInstance.get('/user-settings/transcription/system-defaults');
  return response.data;
}

/**
 * Group languages into "common" and "other" categories for better UI organization
 */
export function groupLanguages(
  allLanguages: Record<string, string>,
  commonCodes: string[]
): { common: LanguageOption[]; other: LanguageOption[] } {
  const commonSet = new Set(commonCodes);
  const common: LanguageOption[] = [];
  const other: LanguageOption[] = [];

  for (const [code, name] of Object.entries(allLanguages)) {
    const option: LanguageOption = { code, name };
    if (commonSet.has(code)) {
      common.push(option);
    } else {
      other.push(option);
    }
  }

  // Sort common languages by their position in commonCodes array
  common.sort((a, b) => commonCodes.indexOf(a.code) - commonCodes.indexOf(b.code));
  // Sort other languages alphabetically by name
  other.sort((a, b) => a.name.localeCompare(b.name));

  return { common, other };
}
