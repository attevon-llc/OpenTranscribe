/**
 * API service for speaker attribute detection settings
 *
 * Manages user preferences for speaker attribute detection including:
 * - Enable/disable attribute detection
 * - Gender prediction from voice
 * - Display preferences for attribute badges on speaker cards
 */

import axiosInstance from '../axios';

/**
 * User speaker attribute detection settings
 */
export interface SpeakerAttributeSettings {
  detection_enabled: boolean;
  gender_detection_enabled: boolean;
  show_attributes_on_cards: boolean;
  /** Automatically ask the AI provider to suggest speaker names after transcription. */
  auto_llm_speaker_identification: boolean;
  /**
   * The layer above the user (platform, instance or organization) that forbids gender
   * inference for the caller's active organization, or null. Read-only; when set,
   * `gender_detection_enabled` is false whatever the user chose.
   */
  gender_locked_by?: string | null;
}

/**
 * System-level defaults for speaker attribute detection
 */
export interface SpeakerAttributeSystemDefaults {
  detection_enabled: boolean;
  gender_detection_enabled: boolean;
  show_attributes_on_cards: boolean;
  auto_llm_speaker_identification: boolean;
}

/**
 * Get user's speaker attribute detection settings
 */
export async function getSpeakerAttributeSettings(): Promise<SpeakerAttributeSettings> {
  const response = await axiosInstance.get('/user-settings/speaker-attributes');
  return response.data;
}

/**
 * Update user's speaker attribute detection settings
 */
export async function updateSpeakerAttributeSettings(
  settings: Partial<SpeakerAttributeSettings>
): Promise<SpeakerAttributeSettings> {
  const response = await axiosInstance.put('/user-settings/speaker-attributes', settings);
  return response.data;
}

/**
 * Reset speaker attribute settings to system defaults
 */
export async function resetSpeakerAttributeSettings(): Promise<void> {
  await axiosInstance.delete('/user-settings/speaker-attributes');
}

/**
 * Get system default values for speaker attribute settings
 */
export async function getSpeakerAttributeSystemDefaults(): Promise<SpeakerAttributeSystemDefaults> {
  const response = await axiosInstance.get('/user-settings/speaker-attributes/system-defaults');
  return response.data;
}
