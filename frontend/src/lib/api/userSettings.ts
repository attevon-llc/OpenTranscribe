/**
 * API service for user settings management
 *
 * This module provides TypeScript interfaces and API methods for managing
 * user settings that persist across sessions and devices.
 */

import axiosInstance from '../axios';

export interface RecordingSettings {
  max_recording_duration: number;
  recording_quality: 'standard' | 'high' | 'maximum';
  auto_stop_enabled: boolean;
}

export interface UpdateRecordingSettingsRequest {
  max_recording_duration?: number;
  recording_quality?: 'standard' | 'high' | 'maximum';
  auto_stop_enabled?: boolean;
}

export interface ResetResponse {
  message: string;
  default_settings: RecordingSettings;
}

/**
 * API service class for user settings operations
 */
export class UserSettingsApi {
  /**
   * Get user's current recording settings from database
   * @returns Promise resolving to current recording settings with defaults for unset values
   */
  static async getRecordingSettings(): Promise<RecordingSettings> {
    const response = await axiosInstance.get('/user-settings/recording');
    return response.data;
  }

  /**
   * Update user's recording settings in database
   * @param settings - Partial settings object with values to update
   * @returns Promise resolving to updated recording settings
   */
  static async updateRecordingSettings(
    settings: UpdateRecordingSettingsRequest
  ): Promise<RecordingSettings> {
    const response = await axiosInstance.put('/user-settings/recording', settings);
    return response.data;
  }

  /**
   * Reset recording settings to system defaults (removes custom settings from database)
   * @returns Promise resolving to reset confirmation and default values
   */
  static async resetRecordingSettings(): Promise<ResetResponse> {
    const response = await axiosInstance.delete('/user-settings/recording');
    return response.data;
  }

  /**
   * Get all user settings (for debugging/admin purposes)
   * @returns Promise resolving to all user settings as key-value pairs
   */
  static async getAllSettings(): Promise<Record<string, string>> {
    const response = await axiosInstance.get('/user-settings/all');
    return response.data;
  }
}

// Default values matching backend
const DEFAULT_RECORDING_SETTINGS: RecordingSettings = {
  max_recording_duration: 120,
  recording_quality: 'high',
  auto_stop_enabled: true,
};

// Helper functions for working with recording settings
export class RecordingSettingsHelper {
  /**
   * Get display label for recording quality
   */
  static getQualityLabel(quality: string): string {
    const labels: Record<string, string> = {
      standard: 'Standard (64 kbps)',
      high: 'High (128 kbps)',
      maximum: 'Maximum (256 kbps)',
    };
    return labels[quality] || quality;
  }

  /**
   * Get display label for recording duration
   */
  static getDurationLabel(minutes: number): string {
    if (minutes < 60) {
      return `${minutes} minutes`;
    } else {
      const hours = Math.floor(minutes / 60);
      return hours === 1 ? '1 hour' : `${hours} hours`;
    }
  }

  /**
   * Validate recording settings
   */
  static validateSettings(settings: Partial<RecordingSettings>): string[] {
    const errors: string[] = [];

    if (settings.max_recording_duration !== undefined) {
      const validDurations = [15, 30, 60, 120, 240, 480];
      if (!validDurations.includes(settings.max_recording_duration)) {
        errors.push('Invalid recording duration. Must be 15, 30, 60, 120, 240, or 480 minutes.');
      }
    }

    if (settings.recording_quality !== undefined) {
      const validQualities = ['standard', 'high', 'maximum'];
      if (!validQualities.includes(settings.recording_quality)) {
        errors.push('Invalid recording quality. Must be standard, high, or maximum.');
      }
    }

    if (settings.auto_stop_enabled !== undefined) {
      if (typeof settings.auto_stop_enabled !== 'boolean') {
        errors.push('Auto-stop setting must be a boolean value.');
      }
    }

    return errors;
  }

  /**
   * Migrate settings from localStorage (for backwards compatibility)
   */
  static migrateFromLocalStorage(): RecordingSettings | null {
    try {
      const localSettings = localStorage.getItem('recordingSettings');
      if (localSettings) {
        const parsed = JSON.parse(localSettings);

        // Map old localStorage format to new API format
        const migrated: RecordingSettings = {
          max_recording_duration:
            parsed.maxRecordingDuration || DEFAULT_RECORDING_SETTINGS.max_recording_duration,
          recording_quality:
            parsed.recordingQuality || DEFAULT_RECORDING_SETTINGS.recording_quality,
          auto_stop_enabled:
            parsed.autoStopEnabled !== undefined
              ? parsed.autoStopEnabled
              : DEFAULT_RECORDING_SETTINGS.auto_stop_enabled,
        };

        // Validate migrated settings
        const errors = this.validateSettings(migrated);
        if (errors.length === 0) {
          return migrated;
        } else {
          console.warn('Invalid settings found in localStorage, using defaults:', errors);
        }
      }
    } catch (error) {
      console.warn('Error migrating recording settings from localStorage:', error);
    }

    return null;
  }

  /**
   * Clean up old localStorage settings
   */
  static cleanupLocalStorage(): void {
    try {
      localStorage.removeItem('recordingSettings');
    } catch (error) {
      console.warn('Error cleaning up localStorage:', error);
    }
  }
}
