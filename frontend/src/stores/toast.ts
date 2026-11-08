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

/** How long after a read-only-demo refusal other error toasts are held back (see `readOnlyRefusal`). */
const READ_ONLY_WINDOW_MS = 2000;
let errorsHeldUntil = 0;
let refusalMessage = '';

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
      // The refusal toast already said it; the failing call site's own generic error that
      // follows within the window is the same event, not a second one.
      if (type === 'error' && Date.now() < errorsHeldUntil && message !== refusalMessage) return;
      const id = `${Date.now()}-${++toastCounter}`;
      const toast: ToastMessage = { id, message, type, duration, ...options };

      // The same sentence twice at once (an interceptor and a call site reporting one failure)
      // is one toast, not a stack.
      let duplicate = false;
      update((toasts) => {
        duplicate = toasts.some(
          (existing) => existing.message === message && existing.type === type
        );
        return duplicate ? toasts : [...toasts, toast];
      });
      if (duplicate) return;

      // Auto-remove after duration
      if (duration > 0) {
        setTimeout(() => {
          this.dismiss(id);
        }, duration);
      }
    },

    /**
     * A write was refused because this is a read-only demo (HTTP 403 `demo_read_only`). Shows
     * the refusal once and holds back other error toasts for a moment, so a call site that
     * reports its own failure does not stack a second message on the same click.
     */
    readOnlyRefusal(message: string) {
      refusalMessage = message;
      errorsHeldUntil = Date.now() + READ_ONLY_WINDOW_MS;
      this.show(message, 'error', 4000);
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
