/**
 * API service for download quality user settings
 *
 * Manages user preferences for URL download quality including:
 * - Video quality selection (best, 4K, 1440p, 1080p, 720p, 480p, 360p)
 * - Audio-only mode
 * - Audio bitrate selection (best, 320kbps, 192kbps, 128kbps)
 */

import axiosInstance from '$lib/axios';

/**
 * System-level download defaults and available options
 */
export interface DownloadSystemDefaults {
  video_quality: string;
  audio_only: boolean;
  audio_quality: string;
  available_video_qualities: Record<string, string>;
  available_audio_qualities: Record<string, string>;
}

/**
 * User download settings
 */
export interface DownloadSettings {
  video_quality: string;
  audio_only: boolean;
  audio_quality: string;
}

/**
 * Request payload for updating download settings
 */
export interface DownloadSettingsUpdate {
  video_quality?: string;
  audio_only?: boolean;
  audio_quality?: string;
}

/**
 * Response from reset endpoint
 */
export interface DownloadSettingsResetResponse {
  message: string;
  default_settings: DownloadSettings;
}

/**
 * Default download settings (client-side fallback)
 */
export const DEFAULT_DOWNLOAD_SETTINGS: DownloadSettings = {
  video_quality: 'best',
  audio_only: false,
  audio_quality: 'best',
};

/**
 * Get user's download settings
 */
export async function getDownloadSettings(): Promise<DownloadSettings> {
  const response = await axiosInstance.get('/user-settings/download');
  return response.data;
}

/**
 * Update user's download settings
 */
export async function updateDownloadSettings(
  settings: DownloadSettingsUpdate
): Promise<DownloadSettings> {
  const response = await axiosInstance.put('/user-settings/download', settings);
  return response.data;
}

/**
 * Reset download settings to system defaults
 */
export async function resetDownloadSettings(): Promise<DownloadSettingsResetResponse> {
  const response = await axiosInstance.delete('/user-settings/download');
  return response.data;
}

/**
 * Get system default values for download settings
 */
export async function getDownloadSystemDefaults(): Promise<DownloadSystemDefaults> {
  const response = await axiosInstance.get('/user-settings/download/system-defaults');
  return response.data;
}
