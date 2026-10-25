/**
 * Reactive LLM Status Store
 *
 * The one client cache of `GET /llm/status`; every AI-gated button reads `isLLMAvailable`.
 * Kept current by: `initialize()` (once per session), a 2-minute poll, a re-check when the
 * page regains attention (tab visible / window focus), and `invalidate()`, which every LLM
 * settings flow calls after a change. `reset()` is for sign-out only: it stops all of that.
 */

import { writable, derived, get } from 'svelte/store';
import type { LLMStatus } from '$lib/services/llmService';
import { llmService } from '$lib/services/llmService';

export interface LLMStatusState {
  status: LLMStatus | null;
  available: boolean;
  checking: boolean;
  lastChecked: Date | null;
}

// Initial state
const initialState: LLMStatusState = {
  status: null,
  available: false,
  checking: false,
  lastChecked: null,
};

const POLL_INTERVAL_MS = 120_000;
/** A burst of focus/visibility events (alt-tab back and forth) costs one request. */
const ATTENTION_RECHECK_MIN_MS = 15_000;

// Main LLM status store with centralized monitoring
function createLLMStatusStore() {
  const { subscribe, set, update } = writable<LLMStatusState>(initialState);
  let monitoringTimer: ReturnType<typeof setInterval> | undefined;
  let initPromise: Promise<void> | null = null;
  let lastAttentionCheck = 0;

  function onAttention(): void {
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    const state = get({ subscribe });
    const now = Date.now();
    const lastChecked = state.lastChecked?.getTime() ?? 0;
    if (
      state.checking ||
      now - Math.max(lastChecked, lastAttentionCheck) < ATTENTION_RECHECK_MIN_MS
    ) {
      return;
    }
    lastAttentionCheck = now;
    void store.refreshStatus();
  }

  function addAttentionListeners(): void {
    if (typeof window === 'undefined') return;
    window.addEventListener('focus', onAttention);
    document.addEventListener('visibilitychange', onAttention);
  }

  function removeAttentionListeners(): void {
    if (typeof window === 'undefined') return;
    window.removeEventListener('focus', onAttention);
    document.removeEventListener('visibilitychange', onAttention);
  }

  const store = {
    subscribe,

    // Initialize the store and start monitoring.
    // Safe to call multiple times: `initPromise` is cleared on failure (so a retry can
    // re-attempt) and in `reset()`, but NOT on success — so after the first successful
    // call, every subsequent call is a no-op that returns the already-resolved promise
    // rather than performing a fresh check. This is deliberate: callers (see
    // `+layout.svelte`'s reactive `$isAuthenticated` block and its `onMount` guard, and
    // `SelectiveReprocessModal.svelte`) use `initialize()` as an idempotent "ensure ready"
    // call fired from multiple places, not as a way to force a refresh. Freshness after
    // the first success is instead kept up by `startMonitoring()`'s 2-minute polling
    // interval and the attention re-check — after changing LLM settings call `invalidate()`.
    initialize() {
      if (!initPromise) {
        initPromise = (async () => {
          try {
            update((state) => ({ ...state, checking: true }));
            const status = await llmService.getStatus(true);

            update((state) => ({
              ...state,
              status,
              available: status.available,
              lastChecked: new Date(),
              checking: false,
            }));

            store.startMonitoring();
          } catch (error) {
            console.error('[LLM Store] Failed to initialize LLM status:', error);
            update((state) => ({ ...state, checking: false, available: false }));
            // Reset so a retry can attempt again
            initPromise = null;
          }
        })();
      }
      return initPromise;
    },

    // Start background monitoring: the poll plus the attention re-check.
    startMonitoring() {
      if (monitoringTimer) clearInterval(monitoringTimer);
      removeAttentionListeners();

      monitoringTimer = setInterval(async () => {
        const currentState = get({ subscribe });
        if (!currentState.checking) {
          await store.refreshStatus();
        }
      }, POLL_INTERVAL_MS);
      addAttentionListeners();
    },

    // Stop monitoring
    stopMonitoring() {
      if (monitoringTimer) {
        clearInterval(monitoringTimer);
        monitoringTimer = undefined;
      }
      removeAttentionListeners();
    },

    /**
     * LLM settings changed (a provider was added, edited, activated, tested or deleted):
     * re-ask the server now and make sure the background re-check is running. The single
     * call every settings flow makes; never `reset()`, which would also stop the re-check
     * and leave every AI button frozen until a page reload.
     */
    async invalidate() {
      if (!monitoringTimer) store.startMonitoring();
      return store.refreshStatus();
    },

    // Refresh status from backend
    async refreshStatus() {
      try {
        update((state) => ({ ...state, checking: true }));
        const status = await llmService.getStatus(true);

        update((state) => ({
          ...state,
          status,
          available: status.available,
          lastChecked: new Date(),
          checking: false,
        }));

        return status;
      } catch (error) {
        console.error('[LLM Store] Error refreshing LLM status:', error);
        update((state) => ({
          ...state,
          checking: false,
          available: false,
          status: {
            available: false,
            user_id: '0',
            provider: null,
            model: null,
            message: 'Unable to check LLM status',
          },
        }));
        return null;
      }
    },

    // Update the LLM status (external)
    setStatus: (status: LLMStatus) => {
      update((state) => ({
        ...state,
        status,
        available: status.available,
        lastChecked: new Date(),
        checking: false,
      }));
    },

    // Set checking state
    setChecking: (checking: boolean) => {
      update((state) => ({ ...state, checking }));
    },

    // Sign-out: back to the initial state with every re-check stopped.
    reset: () => {
      set(initialState);
      store.stopMonitoring();
      initPromise = null;
      lastAttentionCheck = 0;
    },
  };

  return store;
}

export const llmStatusStore = createLLMStatusStore();

// Derived stores for common use cases
export const isLLMAvailable = derived(llmStatusStore, ($llmStatus) => {
  return $llmStatus.available;
});
export const llmProvider = derived(
  llmStatusStore,
  ($llmStatus) => $llmStatus.status?.provider || null
);
