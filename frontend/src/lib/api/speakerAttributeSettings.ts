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
}

/**
 * System-level defaults for speaker attribute detection
 */
export interface SpeakerAttributeSystemDefaults {
  detection_enabled: boolean;
  gender_detection_enabled: boolean;
  show_attributes_on_cards: boolean;
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
