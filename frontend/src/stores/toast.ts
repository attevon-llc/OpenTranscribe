import { writable } from 'svelte/store';

/** A single clickable affordance on a toast (issue #788 — e.g. a disabled-until-elapsed retry). */
export interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface ToastMessage {
  id: string;
  message: string;
  type: 'success' | 'error' | 'info' | 'warning';
  duration?: number;
  /**
   * Seconds until a rate-limited action can be retried (issue #788), parsed
   * from a `Retry-After` header. Rendered as a static "try again in..." hint
   * via `formatting.ts`'s `retryWaitLabel` — never a live countdown, see that
   * function's doc comment for why.
   */
  retryAfterSeconds?: number;
  /** Optional action button, e.g. a disabled-until-elapsed "Retry" (issue #788). */
  action?: ToastAction;
}

/** Optional extras beyond message/type/duration — see {@link ToastMessage}. */
export interface ToastOptions {
  retryAfterSeconds?: number;
  action?: ToastAction;
}

let toastCounter = 0;

function createToastStore() {
  const { subscribe, update, set } = writable<ToastMessage[]>([]);

  return {
    subscribe,

    show(
      message: string,
      type: ToastMessage['type'] = 'success',
      duration = 3000,
      options?: ToastOptions
    ) {
      const id = `${Date.now()}-${++toastCounter}`;
      const toast: ToastMessage = { id, message, type, duration, ...options };

      update((toasts) => [...toasts, toast]);

      // Auto-remove after duration
      if (duration > 0) {
        setTimeout(() => {
          this.dismiss(id);
        }, duration);
      }
    },

    dismiss(id: string) {
      update((toasts) => toasts.filter((t) => t.id !== id));
    },

    /**
     * Clear all toasts. Called on login/logout to prevent stale toasts
     * from a previous user's session from leaking into the next session.
     */
    clear() {
      set([]);
    },

    success(message: string, duration?: number) {
      this.show(message, 'success', duration);
    },

    error(message: string, duration?: number, options?: ToastOptions) {
      // Errors get longer duration (8 seconds) to allow reading
      this.show(message, 'error', duration ?? 8000, options);
    },

    warning(message: string, duration?: number, options?: ToastOptions) {
      this.show(message, 'warning', duration, options);
    },

    info(message: string, duration?: number) {
      this.show(message, 'info', duration);
    },
  };
}

export const toastStore = createToastStore();
