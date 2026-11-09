/**
 * API service for audio extraction user settings
 */

import axiosInstance from '$lib/axios';

export interface AudioExtractionSettings {
  auto_extract_enabled: boolean;
  extraction_threshold_mb: number;
  remember_choice: boolean;
  show_modal: boolean;
}

/**
 * Get user's audio extraction settings
 */
export async function getAudioExtractionSettings(): Promise<AudioExtractionSettings> {
  const response = await axiosInstance.get('/user-settings/audio-extraction');
  return response.data;
}

/**
 * Update user's audio extraction settings
 */
export async function updateAudioExtractionSettings(
  settings: Partial<AudioExtractionSettings>
): Promise<AudioExtractionSettings> {
  const response = await axiosInstance.put('/user-settings/audio-extraction', settings);
  return response.data;
}
