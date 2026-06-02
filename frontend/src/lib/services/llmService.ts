/**
 * LLM Service for checking availability and status
 */

import axiosInstance from '$lib/axios';
import { get } from 'svelte/store';
import { t } from '$stores/locale';
import { getErrorMessage } from '$lib/utils/apiError';

export interface LLMStatus {
  available: boolean;
  user_id: string; // UUID
  provider?: string | null;
  model?: string | null;
  message: string;
}

export interface LLMProviders {
  providers: string[];
  total: number;
  message: string;
}

export interface LLMConnectionTest {
  success: boolean;
  message: string;
  provider?: string;
  model?: string;
  details?: string;
}

export class LLMService {
  private static instance: LLMService;
  private statusCache: LLMStatus | null = null;
  private lastCheck: number = 0;
  private readonly CACHE_DURATION = 60000; // 1 minute for better UX
  private readonly FAST_CACHE_DURATION = 10000; // 10 seconds for recent failures

  private constructor() {}

  static getInstance(): LLMService {
    if (!LLMService.instance) {
      LLMService.instance = new LLMService();
    }
    return LLMService.instance;
  }

  /**
   * Check if LLM services are available (with caching)
   */
  async isAvailable(forceRefresh = false): Promise<boolean> {
    try {
      const status = await this.getStatus(forceRefresh);
      return status.available;
    } catch (error) {
      console.warn('LLM availability check failed:', error);
      return false;
    }
  }

  /**
   * Get detailed LLM status
   */
  async getStatus(forceRefresh = false): Promise<LLMStatus> {
    const now = Date.now();

    // Use different cache durations based on last result
    const cacheDuration = this.statusCache?.available
      ? this.CACHE_DURATION
      : this.FAST_CACHE_DURATION;

    // Return cached status if still valid
    if (!forceRefresh && this.statusCache && now - this.lastCheck < cacheDuration) {
      return this.statusCache;
    }

    try {
      const response = await axiosInstance.get('/llm/status');
      this.statusCache = response.data;
      this.lastCheck = now;
      return this.statusCache as LLMStatus;
    } catch (error: unknown) {
      console.error('[LLM Service] Error getting LLM status:', error);

      // Return default unavailable status on error
      const errorStatus: LLMStatus = {
        available: false,
        user_id: '0',
        provider: null,
        model: null,
        message: getErrorMessage(error, get(t)('llm.unableToCheckStatus')),
      };

      this.statusCache = errorStatus;
      this.lastCheck = now;
      return errorStatus;
    }
  }

  /**
   * Get list of supported LLM providers
   */
  async getProviders(): Promise<LLMProviders> {
    try {
      const response = await axiosInstance.get('/llm/providers');
      return response.data;
    } catch (error: unknown) {
      console.error('Error getting LLM providers:', error);
      throw new Error(getErrorMessage(error, get(t)('llm.providersLoadFailed')));
    }
  }

  /**
   * Test connection to the configured LLM
   */
  async testConnection(): Promise<LLMConnectionTest> {
    try {
      const response = await axiosInstance.post('/llm/test-connection');
      return response.data;
    } catch (error: unknown) {
      console.error('Error testing LLM connection:', error);
      return {
        success: false,
        message: getErrorMessage(error, get(t)('llm.connectionTestFailed')),
        details: get(t)('llm.unableToReach'),
      };
    }
  }

  /**
   * Clear cached status to force refresh on next check
   */
  clearCache(): void {
    this.statusCache = null;
    this.lastCheck = 0;
  }

  /**
   * Invalidate cache and get fresh status (used when settings change)
   */
  async refreshStatus(): Promise<LLMStatus> {
    this.clearCache();
    return await this.getStatus(true);
  }

  /**
   * Get user-friendly message about LLM availability
   */
  getAvailabilityMessage(status: LLMStatus): string {
    if (status.available) {
      return get(t)('llm.featuresAvailable', {
        provider: status.provider,
        model: status.model,
      });
    } else {
      return status.message || get(t)('llm.featuresUnavailable');
    }
  }

  /**
   * Get CSS class for status indicator
   */
  getStatusClass(available: boolean): string {
    return available ? 'llm-available' : 'llm-unavailable';
  }
}

// Export singleton instance
export const llmService = LLMService.getInstance();
