/**
 * Network Connectivity Store
 * Monitors internet connectivity status using browser's native Navigator API
 * and online/offline events for real-time updates
 */

import { writable, derived } from 'svelte/store';

interface NetworkState {
  online: boolean;
  lastChecked: Date;
}

// Initial state based on current browser status
const initialState: NetworkState = {
  online: typeof navigator !== 'undefined' ? navigator.onLine : true,
  lastChecked: new Date(),
};

// Main network status store
function createNetworkStore() {
  const { subscribe, set, update } = writable<NetworkState>(initialState);
  let initialized = false;

  const store = {
    subscribe,

    // Initialize event listeners
    initialize() {
      if (initialized || typeof window === 'undefined') {
        return;
      }

      // Update state when browser detects connection change
      const handleOnline = () => {
        update((state) => ({
          ...state,
          online: true,
          lastChecked: new Date(),
        }));
      };

      const handleOffline = () => {
        update((state) => ({
          ...state,
          online: false,
          lastChecked: new Date(),
        }));
      };

      // Listen to browser events
      window.addEventListener('online', handleOnline);
      window.addEventListener('offline', handleOffline);

      // Set initial state
      set({
        online: navigator.onLine,
        lastChecked: new Date(),
      });

      initialized = true;

      // Return cleanup function
      return () => {
        window.removeEventListener('online', handleOnline);
        window.removeEventListener('offline', handleOffline);
        initialized = false;
      };
    },

    // Manual refresh of status
    refresh() {
      if (typeof navigator !== 'undefined') {
        update((state) => ({
          ...state,
          online: navigator.onLine,
          lastChecked: new Date(),
        }));
      }
    },
  };

  return store;
}

export const networkStore = createNetworkStore();

// Derived store for easy access to online status
export const isOnline = derived(networkStore, ($network) => $network.online);
