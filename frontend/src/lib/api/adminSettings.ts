/**
 * API client for admin settings management
 */

import axiosInstance from '../axios';

export interface RetryConfig {
  max_retries: number;
  retry_limit_enabled: boolean;
}

export interface RetryConfigUpdate {
  max_retries?: number;
  retry_limit_enabled?: boolean;
}

export interface GarbageCleanupConfig {
  garbage_cleanup_enabled: boolean;
  max_word_length: number;
}

export interface GarbageCleanupConfigUpdate {
  garbage_cleanup_enabled?: boolean;
  max_word_length?: number;
}

export class AdminSettingsApi {
  private static readonly BASE_PATH = '/admin';

  /**
   * Get retry configuration (admin only)
   */
  static async getRetryConfig(): Promise<RetryConfig> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/settings/retry-config`);
    return response.data;
  }

  /**
   * Update retry configuration (admin only)
   */
  static async updateRetryConfig(config: RetryConfigUpdate): Promise<RetryConfig> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/settings/retry-config`, config);
    return response.data;
  }

  /**
   * Get garbage cleanup configuration (admin only)
   */
  static async getGarbageCleanupConfig(): Promise<GarbageCleanupConfig> {
    const response = await axiosInstance.get(`${this.BASE_PATH}/settings/garbage-cleanup`);
    return response.data;
  }

  /**
   * Update garbage cleanup configuration (admin only)
   */
  static async updateGarbageCleanupConfig(
    config: GarbageCleanupConfigUpdate
  ): Promise<GarbageCleanupConfig> {
    const response = await axiosInstance.put(`${this.BASE_PATH}/settings/garbage-cleanup`, config);
    return response.data;
  }
}
