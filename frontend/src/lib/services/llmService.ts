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

interface LLMProviders {
  providers: string[];
  total: number;
  message: string;
}

interface LLMConnectionTest {
  success: boolean;
  message: string;
  provider?: string;
  model?: string;
  details?: string;
}

class LLMService {
  private static instance: LLMService;
  private statusCache: LLMStatus | null = null;
  private lastCheck: number = 0;
  private readonly CACHE_DURATION = 60000; // 1 minute for better UX
  private readonly FAST_CACHE_DURATION = 10000; // 10 seconds for recent failures
  // Shared in-flight request for concurrent cache-miss callers (see `getStatus`).
  private fetchPromise: Promise<LLMStatus> | null = null;
  // Bumped by `clearCache()`; see its docstring and the guard in `getStatus`.
  private generation = 0;

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

    // Concurrent callers hitting a cold/expired cache must share ONE in-flight
    // request rather than each firing their own `axiosInstance.get`: promise
    // resolution order is not guaranteed to match request-issue order, so an
    // earlier request that happens to resolve LAST would overwrite a newer
    // cached value with stale data (BC-32). `forceRefresh` deliberately does
    // NOT join this shared promise — a forced caller (e.g. `refreshStatus()`
    // right after `clearCache()`, used when settings change) wants a request
    // issued *after* its own call, not a possibly-older one already in flight.
    if (!forceRefresh && this.fetchPromise) {
      return this.fetchPromise;
    }

    // Captured so a fetch still in flight when `clearCache()` bumps the
    // generation (e.g. settings changed mid-request) can detect it and skip
    // writing its now-stale result over whatever a subsequent forced refetch
    // already wrote. `clearCache()` has no production callers today besides
    // `refreshStatus()` below, but it is a public method on a shared
    // singleton — the same shape of bug bit `configService`'s reset path
    // (BC-4) once a second caller was added, so this guards it up front
    // rather than leaving it as a "future dedup bug" like the one this file
    // is otherwise fixing.
    const startedAtGeneration = this.generation;

    // Set (below, synchronously, before this IIFE's first `await` yields) only
    // when this fetch is the one occupying `this.fetchPromise`. Concurrent
    // non-forced callers join that same slot instead of starting a new fetch
    // (the check above), so at most one in-flight fetch ever owns it — a
    // plain flag is enough; there is no other fetch that could have replaced
    // it out from under us by the time `finally` runs.
    let ownsSharedSlot = false;

    const fetchPromise = (async (): Promise<LLMStatus> => {
      try {
        const response = await axiosInstance.get('/llm/status');
        const status = response.data as LLMStatus;
        if (this.generation === startedAtGeneration) {
          this.statusCache = status;
          this.lastCheck = Date.now();
        }
        return status;
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

        if (this.generation === startedAtGeneration) {
          this.statusCache = errorStatus;
          this.lastCheck = Date.now();
        }
        return errorStatus;
      } finally {
        if (ownsSharedSlot) {
          this.fetchPromise = null;
        }
      }
    })();

    if (!forceRefresh) {
      this.fetchPromise = fetchPromise;
      ownsSharedSlot = true;
    }

    return fetchPromise;
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
      throw new Error(getErrorMessage(error, get(t)('llm.providersLoadFailed')), { cause: error });
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
   * Clear cached status to force refresh on next check.
   *
   * Bumps `generation` so a fetch already in flight at the moment of the clear
   * discards its own result instead of writing it into the cache after a
   * subsequent forced refetch (see `getStatus`) — otherwise `refreshStatus()`
   * could observe its own fresh fetch immediately clobbered by a stale one
   * that started before the clear.
   */
  clearCache(): void {
    this.statusCache = null;
    this.lastCheck = 0;
    this.generation++;
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
